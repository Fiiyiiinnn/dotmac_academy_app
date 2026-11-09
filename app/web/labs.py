# Ported at new-composition genesis (web layer wave).
# app/web/labs.py
"""Student lab routes.

This module currently hosts only the auth-gated console proxy (Task 8). The
student-facing lab pages (launch / status / check) land in Task 9 in this same
file.

Security model: a console may only be reached by the authenticated **owner** of
a **tenant-scoped** LabInstance. The gate (`_authorize_console`) is enforced
*before* any byte is proxied. It is factored out so the WebSocket (ttyd) path
can reuse it verbatim when ttyd ports are wired on the `.42` VM (Task 12).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID

import httpx
import websockets
from dotmac_kernel.middleware.tenant import TenantResolverMiddleware
from dotmac_kernel.models import Tenant
from dotmac_kernel.session_runtime import resolve_database_runtime
from dotmac_learning import LabResultRead, ScoreFact, read_lab_result
from dotmac_learning.models import Activity
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from sqlalchemy import desc, select
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketDisconnect

from app.api.deps import get_db, require_tenant
from app.config import settings
from app.models.lab import LabInstance, LabOperation, LabTemplate
from app.services import lab_console_activity, lab_lifecycle, web_auth
from app.services.entitlements import require_course_open
from app.services.identity import AcademyCurrentAccount
from app.services.lab_content import render_instance_instructions
from app.services.lab_operations import OPEN_STATES, enqueue
from app.services.web_auth import require_web_user
from app.web import webfig_adapter
from app.web.console_document import read_reviewed_document, require_console_principal
from app.web.console_proxy import (
    MAX_DEVICE_COOKIES,
    WebFigRootRewriter,
    console_upstream_timeout,
    device_cookie_header,
    rewrite_location,
    rewrite_set_cookie,
    safe_mgmt_address,
    safe_subpath,
    should_rewrite_webfig_body,
)
from app.web.templating import templates

logger = logging.getLogger(__name__)

#: Shown in place of instructions before a learner has an instance (queued,
#: provisioning, active, or error — any non-reaped LabInstance) to render
#: against: there is no seed yet, so there is nothing to interpolate.
_NO_INSTANCE_INSTRUCTIONS = "<p>Launch the lab to view your instance-specific instructions.</p>"

#: Instance states in which the learner has (or is getting) a lab, so the
#: device logins are useful. ``error`` and ``reaped`` show none.
_ACCESS_VISIBLE_STATUSES = frozenset({"queued", "provisioning", "resetting", "active"})


class _TenantContext(Protocol):
    """The common request-tenant shape shared by Academy and kernel models."""

    id: UUID


router = APIRouter(dependencies=[Depends(require_tenant)])

# WebSocket routes cannot carry the HTTP-only `require_tenant` dependency
# (BaseHTTPMiddleware does not run for the websocket scope, and FastAPI does not
# inject a `Request` into ws dependencies), so the ttyd console socket lives on a
# dependency-free router and resolves tenant/person inline — see `console_ws`.
ws_router = APIRouter()

# Reuse the app's own Host→Tenant resolver verbatim for the websocket scope.
_tenant_resolver = TenantResolverMiddleware(app=None)  # type: ignore[arg-type]

# Hop-by-hop headers must not be forwarded across a proxy (RFC 7230 §6.1).
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)


def _console_target(instance: LabInstance, node: str) -> str | None:
    """Resolve the upstream console URL for `node`, or None if unavailable.

    - RouterOS (kind == "vr-ros" or starting with "vr-") → webfig at the node's
      mgmt address: ``http://{mgmt}/``.
    - Linux → ttyd on the LAB host: ``http://{LAB_CONSOLE_HOST}:{port}/``
      (loopback when the worker is co-located; the lab host's tunnel address
      when it runs on a separate KVM host).
    """
    spec = (instance.consoles or {}).get(node)
    if not spec:
        return None
    kind = str(spec.get("kind", ""))
    if kind == "vr-ros" or kind.startswith("vr-"):
        mgmt = safe_mgmt_address(spec.get("mgmt"))
        return f"http://{mgmt}/" if mgmt else None
    port = spec.get("port")
    return f"http://{settings.lab_console_host}:{port}/" if port else None


def _require_instance_entitlement(
    db: Session,
    tenant: _TenantContext,
    person: AcademyCurrentAccount,
    instance: LabInstance,
) -> None:
    """Re-check the owner is STILL entitled to the lab's course.

    Ownership alone is not enough: a learner who launched a lab while entitled may
    since have been dropped/suspended, had the offering window close, or the course
    unpublished. Without this, a now-non-entitled owner could keep grading/resetting
    the lab and holding a live console until the instance is reaped.
    """
    act = db.scalars(
        select(Activity).where(Activity.id == instance.activity_id).where(Activity.tenant_id == tenant.id)
    ).first()
    if act is None:
        raise HTTPException(status_code=404)
    require_course_open(db, tenant_id=tenant.id, person_id=person.id, course_id=act.course_id)


def _console_live(instance: LabInstance) -> bool:
    """The one definition of "this instance's consoles are reachable".

    Stored mgmt IPs / ttyd ports are only meaningful while the instance's own
    runtime is live; after reap/error/reset they may belong to another learner.
    Shared by the console gate and the status template so a link is shown only
    when following it can succeed.
    """
    return instance.status == "active" and instance.error is None and instance.runtime_presence == "present"


templates.env.globals["lab_console_live"] = _console_live


def _authorize_console(
    db: Session,
    tenant: _TenantContext,
    person: AcademyCurrentAccount,
    instance_id: UUID,
    node: str,
) -> str:
    """Security gate shared by the HTTP (and future WebSocket) console paths.

    Returns the upstream target URL, or raises:
      404 — instance not found in this tenant, not live, or node/target missing.
      403 — instance exists in-tenant but is owned by another person.

    Loads via a tenant-filtered select (never ``db.get(PK)``) so a cross-tenant
    id can never be resolved.
    """
    instance = db.scalars(
        select(LabInstance).where(LabInstance.id == instance_id).where(LabInstance.tenant_id == tenant.id)
    ).first()
    if instance is None:
        raise HTTPException(status_code=404)
    if instance.person_id != person.id:
        raise HTTPException(status_code=403)
    _require_instance_entitlement(db, tenant, person, instance)
    if not _console_live(instance):
        raise HTTPException(status_code=404)
    target = _console_target(instance, node)
    if target is None:
        raise HTTPException(status_code=404)
    return target


# Methods the console proxy accepts. RouterOS WebFig polls and submits through
# ``POST jsproxy``; HEAD is harmless. Nothing else (PUT/DELETE/PATCH/OPTIONS/...).
_CONSOLE_METHODS = ["GET", "HEAD", "POST"]

# Request headers forwarded to the device. An allow-list, not a deny-list: the
# learner's Cookie (Academy session + CSRF token), Authorization, X-CSRF-Token,
# Origin/Referer and X-Forwarded-* are never sent to a lab node.
_FORWARD_REQUEST_HEADERS = frozenset(
    {
        "accept",
        "accept-encoding",
        "accept-language",
        "cache-control",
        "content-type",
        "if-modified-since",
        "if-none-match",
        "pragma",
        "range",
        "user-agent",
    }
)


def _new_upstream_client() -> httpx.AsyncClient:
    """Upstream client with bounded setup and WebFig-compatible long polling."""
    return httpx.AsyncClient(timeout=console_upstream_timeout(), follow_redirects=False)


async def _read_capped_body(request: Request) -> bytes:
    cap = settings.lab_console_max_body_bytes
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > cap):
        raise HTTPException(status_code=413, detail="request body too large")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > cap:
            raise HTTPException(status_code=413, detail="request body too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _note_console_use(db: Session, tenant: _TenantContext, instance_id: UUID) -> None:
    """Record console use for the idle reaper; never lets a failure reach the proxy.

    Call only AFTER ``_authorize_console`` succeeded. A savepoint keeps a failed
    write from poisoning the request's session.
    """
    try:
        with db.begin_nested():
            lab_console_activity.record(db, tenant_id=tenant.id, instance_id=instance_id)
    except Exception as exc:
        lab_console_activity.warn_failure(instance_id, exc)


async def _proxy_http(
    request: Request, target: str, *, webfig: bool = False, reviewed_document: bool = False
) -> Response:
    """Reverse-proxy an HTTP request to `target`, streaming the response back.

    Callers must have run ``_authorize_console`` first; ``target`` is built from
    the instance's own node only. Only allow-listed headers go upstream, the
    body is capped, and device ``Set-Cookie``/redirects are confined to the
    proxy prefix.
    """
    if request.method not in _CONSOLE_METHODS:
        raise HTTPException(status_code=405)
    if webfig and reviewed_document:
        raise HTTPException(status_code=502, detail="invalid console proxy mode")
    fwd_headers = {k: v for k, v in request.headers.items() if k.lower() in _FORWARD_REQUEST_HEADERS}
    asset = None
    asset_path = urlsplit(target).path or "/"
    if webfig and not webfig_adapter.is_dynamic_path(asset_path):
        asset = webfig_adapter.load_manifest()["assets"].get(asset_path)
        # Source and schema assets must belong to the reviewed closure. Unknown
        # passive resources are additionally checked by response media type.
        if asset is None and webfig_adapter.active_content(None, asset_path):
            raise webfig_adapter.refused()
        if asset is not None:
            if request.method not in {"GET", "HEAD"}:
                raise webfig_adapter.refused()
            for name in ("range", "if-modified-since", "if-none-match"):
                fwd_headers.pop(name, None)
            fwd_headers["accept-encoding"] = "gzip"
    if reviewed_document:
        # Always request the complete identity representation for byte validation.
        for header in ("range", "if-modified-since", "if-none-match", "accept-encoding"):
            fwd_headers.pop(header, None)
        fwd_headers["accept-encoding"] = "identity"
    cookie = device_cookie_header(request)
    if cookie:
        fwd_headers["cookie"] = cookie
    body = await _read_capped_body(request) if request.method == "POST" else b""
    pp = request.scope.get("path_params") or {}
    prefix = f"/labs/instances/{pp.get('instance_id')}/console/{pp.get('node')}"
    client = _new_upstream_client()
    try:
        upstream_req = client.build_request(
            "GET" if asset is not None else request.method,
            target,
            headers=fwd_headers,
            content=body,
            params=request.query_params.multi_items(),
        )
        upstream = await client.send(upstream_req, stream=True)
    except BaseException as exc:
        await client.aclose()
        if not isinstance(exc, httpx.HTTPError):
            raise
        logger.warning("console upstream request failed (%s): %s", target, exc)
        raise HTTPException(status_code=502, detail="lab console unreachable") from exc
    if reviewed_document:
        try:
            if upstream.status_code != 200:
                raise HTTPException(status_code=502, detail="lab console document unavailable")
            document = await read_reviewed_document(upstream)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="lab console unreachable") from exc
        finally:
            try:
                await upstream.aclose()
            finally:
                await client.aclose()
        response = Response(document, media_type="text/html", headers={"cache-control": "no-store"})
        # Multiple enforcing policies intersect; preserve every upstream instance
        # so the Kernel document grant cannot weaken a device policy.
        response.raw_headers.extend(
            (key, value) for key, value in upstream.headers.raw if key.lower() == b"content-security-policy"
        )
        return response
    adapted_body = None
    try:
        if webfig and not webfig_adapter.is_dynamic_path(asset_path):
            if asset is None:
                if upstream.status_code in {301, 302, 303, 307, 308}:
                    # Keep the confined Location and cookies, never an unpinned
                    # redirect body. This also closes the upstream before headers.
                    adapted_body = b""
                elif not webfig_adapter.passive_content(upstream.headers.get("content-type")):
                    raise webfig_adapter.refused()
            if asset is not None:
                source = await webfig_adapter.read_asset(upstream, asset)
                adapted_body = webfig_adapter.transform_asset(source, asset, prefix)
    except BaseException:
        try:
            await upstream.aclose()
        finally:
            await client.aclose()
        raise
    if adapted_body is not None:
        try:
            await upstream.aclose()
        finally:
            await client.aclose()
    resp_headers = {
        k: v
        for k, v in upstream.headers.items()
        if k.lower() not in _HOP_BY_HOP and k.lower() not in ("set-cookie", "location")
    }
    if webfig and webfig_adapter.is_dynamic_path(asset_path):
        # Encrypted traffic and downloads stay byte-for-byte opaque, but can
        # never become an alternate same-origin executable source endpoint.
        resp_headers["content-type"] = "application/octet-stream"
        resp_headers["x-content-type-options"] = "nosniff"
        resp_headers["content-disposition"] = "attachment"
    if webfig and asset is None and not webfig_adapter.is_dynamic_path(asset_path):
        # Passive media must not become script through MIME sniffing.
        resp_headers["x-content-type-options"] = "nosniff"
    location = upstream.headers.get("location")
    if location is not None:
        rewritten = rewrite_location(location, target, prefix, preserve_fragment=webfig)
        if rewritten is not None:
            resp_headers["location"] = rewritten

    rewrite_body = (
        not webfig and upstream.status_code != 206 and should_rewrite_webfig_body(upstream.headers.get("content-type"))
    )
    if adapted_body is not None:
        for header in (
            "accept-ranges",
            "content-encoding",
            "content-md5",
            "content-range",
            "digest",
            "etag",
            "last-modified",
        ):
            resp_headers.pop(header, None)
        resp_headers["cache-control"] = "no-store"
    if rewrite_body:
        # ``aiter_bytes`` decodes transfer/content encodings before the path
        # rewrite, so validators for the device's original bytes cannot survive.
        for header in ("accept-ranges", "content-encoding", "content-md5", "content-range", "digest", "etag"):
            resp_headers.pop(header, None)

    async def _body_iter():
        try:
            if not rewrite_body:
                async for chunk in upstream.aiter_raw():
                    yield chunk
                return

            rewriter = WebFigRootRewriter(prefix)
            async for chunk in upstream.aiter_bytes():
                rewritten_chunk = rewriter.feed(chunk)
                if rewritten_chunk:
                    yield rewritten_chunk
            final_chunk = rewriter.finish()
            if final_chunk:
                yield final_chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    if adapted_body is not None:
        response = Response(
            b"" if request.method == "HEAD" else adapted_body,
            status_code=upstream.status_code,
            headers=resp_headers,
        )
        response.headers["content-length"] = str(len(adapted_body))
    else:
        response = StreamingResponse(
            _body_iter(),
            status_code=upstream.status_code,
            headers=resp_headers,
            media_type=upstream.headers.get("content-type"),
        )
    # httpx's mapping interface comma-joins duplicate values, which would change
    # the meaning of CSP. Preserve each raw policy as an independent restriction.
    csp_names = {b"content-security-policy", b"content-security-policy-report-only"}
    response.raw_headers = [(k, v) for k, v in response.raw_headers if k.lower() not in csp_names]
    response.raw_headers.extend((k.lower(), v) for k, v in upstream.headers.raw if k.lower() in csp_names)
    secure = request.url.scheme == "https"
    for raw in upstream.headers.get_list("set-cookie")[:MAX_DEVICE_COOKIES]:
        cookie_out = rewrite_set_cookie(raw, prefix, secure)
        if cookie_out:
            response.headers.append("set-cookie", cookie_out)
    return response


# ---------------------------------------------------------------------------
# Student lab portal (Task 9): launch / status / check / reset.
# All routes are tenant-filtered + ownership-checked; mutations are htmx forms
# that rely on the global CSRF injector. NO mid-handler commit (get_db owns it).
# ---------------------------------------------------------------------------


def _lab_activity(db: Session, tenant: Tenant, activity_id: UUID) -> Activity:
    """Load a tenant-scoped lab Activity or 404."""
    act = db.scalars(
        select(Activity)
        .where(Activity.id == activity_id)
        .where(Activity.tenant_id == tenant.id)
        .where(Activity.type == "lab")
    ).first()
    if act is None:
        raise HTTPException(status_code=404)
    return act


def _lab_template(db: Session, tenant: Tenant, activity_id: UUID) -> LabTemplate:
    """Load the LabTemplate for a tenant-scoped activity or 404."""
    tpl = db.scalars(
        select(LabTemplate).where(LabTemplate.activity_id == activity_id).where(LabTemplate.tenant_id == tenant.id)
    ).first()
    if tpl is None:
        raise HTTPException(status_code=404)
    return tpl


def _current_instance(
    db: Session, tenant: Tenant, person: AcademyCurrentAccount, activity_id: UUID
) -> LabInstance | None:
    """Latest non-reaped instance for this person+activity, if any."""
    return db.scalars(
        select(LabInstance)
        .where(LabInstance.tenant_id == tenant.id)
        .where(LabInstance.activity_id == activity_id)
        .where(LabInstance.person_id == person.id)
        .where(LabInstance.status != "reaped")
        .order_by(desc(LabInstance.created_at))
    ).first()


def _owned_instance(db: Session, tenant: Tenant, person: AcademyCurrentAccount, instance_id: UUID) -> LabInstance:
    """Load a tenant-scoped instance, assert ownership (404/403) AND current
    entitlement to the lab's course (so revocation propagates to live instances)."""
    inst = db.scalars(
        select(LabInstance).where(LabInstance.id == instance_id).where(LabInstance.tenant_id == tenant.id)
    ).first()
    if inst is None:
        raise HTTPException(status_code=404)
    if inst.person_id != person.id:
        raise HTTPException(status_code=403)
    _require_instance_entitlement(db, tenant, person, inst)
    return inst


def _operation_score(
    db: Session, tenant: Tenant, person: AcademyCurrentAccount, instance: LabInstance, operation: LabOperation
) -> ScoreFact | None:
    """Read only the durable result inside this worker operation's window."""
    if (
        operation.tenant_id != tenant.id
        or operation.instance_id != instance.id
        or instance.tenant_id != tenant.id
        or instance.person_id != person.id
        or operation.kind != "check"
        or operation.state != "succeeded"
        or operation.claimed_at is None
        or operation.finished_at is None
        or operation.finished_at < operation.claimed_at
    ):
        return None
    return read_lab_result(
        db,
        LabResultRead(
            tenant_id=tenant.id,
            person_id=person.id,
            activity_id=instance.activity_id,
            instance_id=instance.id,
            claimed_at=operation.claimed_at,
            finished_at=operation.finished_at,
        ),
    )


def _result_context(db: Session, tenant: Tenant, person: AcademyCurrentAccount, instance: LabInstance) -> dict:
    """Recover current work and the last saved check for this runtime journey.

    A deploy request (including a reset that subsequently fails) invalidates the
    old live-configuration display. Academic submissions remain untouched.
    """
    operations = (
        select(LabOperation).where(LabOperation.tenant_id == tenant.id).where(LabOperation.instance_id == instance.id)
    )
    # Open work is authoritative even if terminal requests share a timestamp.
    latest = db.scalars(operations.where(LabOperation.state.in_(OPEN_STATES))).first()
    if latest is None:
        recent = list(db.scalars(operations.order_by(LabOperation.requested_at.desc()).limit(2)))
        latest = recent[0] if recent else None
        if len(recent) == 2 and recent[0].requested_at == recent[1].requested_at:
            latest = None  # UUID order cannot establish which action happened last.
    deploy = db.scalars(
        operations.where(LabOperation.kind == "deploy").order_by(
            LabOperation.requested_at.desc(), LabOperation.id.desc()
        )
    ).first()
    saved_query = operations.where(LabOperation.kind == "check").where(LabOperation.state == "succeeded")
    if deploy is not None:
        saved_query = saved_query.where(LabOperation.requested_at > deploy.requested_at)
    saved_checks = list(db.scalars(saved_query.order_by(LabOperation.requested_at.desc()).limit(2)))
    saved = saved_checks[0] if saved_checks else None
    if len(saved_checks) == 2 and saved_checks[0].requested_at == saved_checks[1].requested_at:
        saved = None  # Never choose a result using random UUID order.
    score = _operation_score(db, tenant, person, instance, saved) if saved else None
    return {
        "recovered_operation": latest
        if latest is not None
        and (
            latest.state in OPEN_STATES
            or latest.state in ("failed", "cancelled")
            or (saved is not None and latest.id == saved.id and score is None)
        )
        else None,
        "saved_score": score,
    }


@router.get("/labs/{activity_id}", response_class=HTMLResponse)
def lab_detail(
    activity_id: UUID,
    request: Request,
    person: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
):
    """Lab page: instructions + Launch (if none/reaped) or status/console/Check."""
    tenant = require_tenant(request)
    act = _lab_activity(db, tenant, activity_id)
    require_course_open(db, tenant_id=tenant.id, person_id=person.id, course_id=act.course_id)
    tpl = _lab_template(db, tenant, activity_id)
    instance = _current_instance(db, tenant, person, activity_id)
    instructions_html = (
        render_instance_instructions(tpl, instance) if instance is not None else _NO_INSTANCE_INSTRUCTIONS
    )
    return templates.TemplateResponse(
        request,
        "labs/detail.html",
        {
            "request": request,
            "activity": act,
            "template": tpl,
            "instance": instance,
            "instructions_html": instructions_html,
            # Device login info is released only alongside the caller's OWN live
            # instance (``_current_instance`` is scoped to this person and
            # tenant), and only the sanitised list the loader validated.
            "device_access": (
                (tpl.access or []) if instance is not None and instance.status in _ACCESS_VISIBLE_STATUSES else []
            ),
            **(_result_context(db, tenant, person, instance) if instance else {}),
        },
    )


@router.post("/labs/{activity_id}/launch", response_class=HTMLResponse)
def lab_launch(
    activity_id: UUID,
    request: Request,
    person: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
):
    """Request a new lab instance (queued/provisioning) → status partial."""
    tenant = require_tenant(request)
    act = _lab_activity(db, tenant, activity_id)
    require_course_open(db, tenant_id=tenant.id, person_id=person.id, course_id=act.course_id)
    tpl = _lab_template(db, tenant, activity_id)
    instance = lab_lifecycle.request_lab(db, tenant_id=tenant.id, person_id=person.id, activity=act, template=tpl)
    resp = templates.TemplateResponse(request, "labs/_status.html", {"request": request, "instance": instance})
    # The instance's seed now exists but the instructions column (rendered
    # server-side into the surrounding page, not this swapped fragment) has
    # not been refreshed — force a full-page reload so the follow-up GET
    # renders instructions interpolated with the real seed instead of the
    # pre-launch placeholder.
    resp.headers["HX-Refresh"] = "true"
    return resp


@router.get("/labs/instances/{instance_id}/status", response_class=HTMLResponse)
def lab_status(
    instance_id: UUID,
    request: Request,
    person: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
):
    """htmx polling target — current instance status + console links when active."""
    tenant = require_tenant(request)
    instance = _owned_instance(db, tenant, person, instance_id)
    return templates.TemplateResponse(
        request,
        "labs/_status.html",
        {"request": request, "instance": instance, **_result_context(db, tenant, person, instance)},
    )


@router.post("/labs/instances/{instance_id}/check", response_class=HTMLResponse)
def lab_check(
    instance_id: UUID,
    request: Request,
    person: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
):
    """Enqueue worker-owned grading and return a polling partial."""
    tenant = require_tenant(request)
    instance = _owned_instance(db, tenant, person, instance_id)
    if instance.status != "active" or instance.error is not None:
        detail = f"status {instance.status!r}" if instance.status != "active" else "an unresolved infrastructure error"
        raise HTTPException(status_code=409, detail=f"cannot check a lab with {detail}")
    operation = enqueue(db, instance=instance, kind="check", requested_by=person.id)
    if operation.kind != "check":
        raise HTTPException(
            status_code=409,
            detail=f"lab operation {operation.kind!r} is already in progress",
        )
    return templates.TemplateResponse(
        request,
        "labs/_operation.html",
        {"request": request, "operation": operation, "instance": instance, "score": None},
    )


@router.post("/labs/instances/{instance_id}/reset", response_class=HTMLResponse)
def lab_reset(
    instance_id: UUID,
    request: Request,
    person: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
):
    """Enqueue a worker-owned destroy-then-deploy reset/retry."""
    tenant = require_tenant(request)
    instance = _owned_instance(db, tenant, person, instance_id)
    retry_in_progress = instance.status == "resetting" or (
        instance.status in ("queued", "provisioning") and instance.error is not None
    )
    if retry_in_progress:
        # Preserve idempotency after the worker commits its reservation. Active
        # resets become ``resetting``; error retries retain their prior error
        # while ``queued`` for capacity or ``provisioning`` after admission.
        # Initial launch/provisioning has no prior error and remains ineligible.
        operation = db.scalars(
            select(LabOperation)
            .where(LabOperation.tenant_id == tenant.id)
            .where(LabOperation.instance_id == instance.id)
            .where(LabOperation.state.in_(OPEN_STATES))
        ).first()
        if operation is not None and operation.kind == "deploy":
            return templates.TemplateResponse(
                request,
                "labs/_operation.html",
                {
                    "request": request,
                    "operation": operation,
                    "instance": instance,
                    "score": None,
                },
            )
    if instance.status not in ("active", "error"):
        raise HTTPException(status_code=409, detail=f"cannot reset a lab with status {instance.status!r}")
    operation = enqueue(db, instance=instance, kind="deploy", requested_by=person.id)
    if operation.kind != "deploy":
        raise HTTPException(
            status_code=409,
            detail=f"lab operation {operation.kind!r} is already in progress",
        )
    return templates.TemplateResponse(
        request,
        "labs/_operation.html",
        {"request": request, "operation": operation, "instance": instance, "score": None},
    )


@router.get("/labs/operations/{operation_id}", response_class=HTMLResponse)
def lab_operation_status(
    operation_id: UUID,
    request: Request,
    person: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
):
    """Poll an owned operation; expose a completed check's durable score."""
    tenant = require_tenant(request)
    operation = db.scalars(
        select(LabOperation).where(LabOperation.id == operation_id).where(LabOperation.tenant_id == tenant.id)
    ).first()
    if operation is None:
        raise HTTPException(status_code=404)
    instance = _owned_instance(db, tenant, person, operation.instance_id)
    score = _operation_score(db, tenant, person, instance, operation)
    # Lifecycle polling owns all of #lab-state. On settlement return the whole
    # status fragment (including retry controls), never nest it in a poll card.
    template = (
        "labs/_status.html"
        if operation.kind != "check" and operation.state not in OPEN_STATES
        else "labs/_operation.html"
    )
    return templates.TemplateResponse(
        request,
        template,
        {
            "request": request,
            "operation": operation,
            "instance": instance,
            "score": score,
            **_result_context(db, tenant, person, instance),
        },
    )


@router.get("/labs/instances/{instance_id}/console/{node}")
async def console(
    instance_id: UUID,
    node: str,
    request: Request,
    person: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
) -> Response:
    """Auth-gated reverse proxy to a lab node's web console (webfig/ttyd)."""
    tenant = require_tenant(request)
    target = _authorize_console(db, tenant, person, instance_id, node)
    require_console_principal(request, person, tenant.id)
    _note_console_use(db, tenant, instance_id)  # opening a console is use
    if not request.url.path.endswith("/") and not target.startswith(f"http://{settings.lab_console_host}:"):
        # WebFig resolves relative URLs (jsproxy, ...) against the page URL, so
        # the page must live at ".../{node}/". Redirect the no-slash form.
        qs = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(f"{request.url.path}/{qs}", status_code=308)
    # ttyd is launched with `-b <base>` so it serves its index UNDER the base path
    # (root 404s); webfig serves at the mgmt root. Reuse _subpath_target("") to
    # land on the correct index for each.
    ttyd = target.startswith(f"http://{settings.lab_console_host}:")
    return await _proxy_http(
        request,
        _subpath_target(target, instance_id, node, ""),
        webfig=not ttyd,
        reviewed_document=ttyd,
    )


def _subpath_target(target: str, instance_id: UUID, node: str, subpath: str) -> str:
    """Build the upstream URL for a console sub-resource.

    ttyd is launched with ``-b /labs/instances/{id}/console/{node}`` so its own
    assets/token/ws live under that base prefix — sub-requests must keep it.
    RouterOS webfig is served at the mgmt root, so its sub-resources hang off ``/``.
    """
    base = target.rstrip("/")
    if target.startswith(f"http://{settings.lab_console_host}:"):
        return f"{base}/labs/instances/{instance_id}/console/{node}/{subpath}"
    return f"{base}/{subpath}"


@router.api_route("/labs/instances/{instance_id}/console/{node}/{subpath:path}", methods=_CONSOLE_METHODS)
async def console_subpath(
    instance_id: UUID,
    node: str,
    subpath: str,
    request: Request,
    person: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
) -> Response:
    """Auth-gated proxy for console sub-resources (ttyd assets/token, webfig).

    GET/HEAD/POST: RouterOS WebFig submits ``POST jsproxy``. The owner, tenant
    and entitlement gate runs for every method before anything is proxied.
    """
    tenant = require_tenant(request)
    target = _authorize_console(db, tenant, person, instance_id, node)
    safe = safe_subpath(subpath)
    webfig = not target.startswith(f"http://{settings.lab_console_host}:")
    if webfig and safe == "webfig" and request.method in {"GET", "HEAD"}:
        # The upstream document's relative scripts/styles require its slash.
        # The owner/tenant/entitlement gate above applies to this redirect too.
        query = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(f"{request.url.path}/{query}", status_code=308)
    if request.method == "GET" and not safe and not webfig:
        require_console_principal(request, person, tenant.id)
        qs = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(f"{request.url.path.rstrip('/')}{qs}", status_code=308)
    if request.method == "GET" and not safe:
        _note_console_use(db, tenant, instance_id)  # trailing-slash index is an opening
    # No heartbeat for non-index sub-resources: they are assets, tokens and
    # WebFig polling, not user action, so an idle open page must not keep a lab
    # alive. Typing is counted on the ttyd websocket; opening a console above.
    return await _proxy_http(
        request,
        _subpath_target(target, instance_id, node, safe),
        webfig=webfig,
    )


async def _proxy_ws(websocket: WebSocket, upstream_url: str) -> None:
    """Accept the client WS and bidirectionally pump it to the upstream ttyd WS.

    Negotiates the ``tty`` subprotocol ttyd requires when the browser offered it,
    and tears both legs down when either side closes.
    """
    offered = websocket.scope.get("subprotocols") or []
    subprotocol = "tty" if "tty" in offered else None
    await websocket.accept(subprotocol=subprotocol)
    try:
        upstream = await websockets.connect(upstream_url, subprotocols=["tty"])  # type: ignore[list-item]
    except Exception as exc:  # upstream ttyd unreachable
        logger.warning("ttyd upstream connect failed (%s): %s", upstream_url, exc)
        await websocket.close(code=1011)
        return

    # ttyd command byte "0" is INPUT (keystrokes/paste); resize, pause and
    # keepalive frames are not user activity, so an idle open tab does not
    # keep a lab alive.
    beat = getattr(websocket.state, "console_heartbeat", None)

    async def client_to_upstream() -> None:
        try:
            while True:
                msg = await websocket.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if msg.get("text") is not None:
                    if beat is not None and msg["text"][:1] == "0":
                        await beat()
                    await upstream.send(msg["text"])
                elif msg.get("bytes") is not None:
                    if beat is not None and msg["bytes"][:1] == b"0":
                        await beat()
                    await upstream.send(msg["bytes"])
        except (WebSocketDisconnect, websockets.ConnectionClosed):
            pass
        finally:
            await upstream.close()

    async def upstream_to_client() -> None:
        try:
            async for data in upstream:
                if isinstance(data, bytes):
                    await websocket.send_bytes(data)
                else:
                    await websocket.send_text(data)
        except websockets.ConnectionClosed:
            pass
        finally:
            try:
                await websocket.close()
            except Exception as exc:  # client already gone — nothing to close
                logger.debug("client ws close failed: %s", exc)

    await asyncio.gather(client_to_upstream(), upstream_to_client())


_heartbeat_tasks: set[asyncio.Task] = set()


def _write_console_heartbeat(tenant_id: UUID, instance_id: UUID) -> None:
    """Short-lived session for one heartbeat write; failures are swallowed.

    Uses the composed database runtime (session factory + tenant scope), the
    same session authority ``console_ws`` itself resolves through.
    """
    runtime = resolve_database_runtime()
    db = runtime.session_factory()
    try:
        with runtime.tenant_scope(db, tenant_id):
            lab_console_activity.record(db, tenant_id=tenant_id, instance_id=instance_id, throttled=False)
            db.commit()
    except Exception as exc:
        lab_console_activity.warn_failure(instance_id, exc)
    finally:
        db.close()


def _console_ws_same_origin(websocket: WebSocket) -> bool:
    """Require the browser origin before touching tenant/session state.

    Uvicorn applies trusted proxy headers to the ASGI scheme. As with the HTTP
    bridge, raw client X-Forwarded-Proto is never independently trusted here.
    """
    origins = websocket.headers.getlist("origin")
    hosts = websocket.headers.getlist("host")
    if len(origins) != 1 or len(hosts) != 1:
        return False
    origin, host = origins[0], hosts[0]
    if any(ord(character) <= 0x20 or ord(character) == 0x7F or character == "\\" for character in origin + host):
        return False
    scheme = {"ws": "http", "wss": "https"}.get(websocket.scope.get("scheme"), websocket.scope.get("scheme"))
    if scheme not in ("http", "https"):
        return False
    try:
        parsed = urlsplit(origin)
        authority = urlsplit(f"//{host}")
        if (
            parsed.scheme not in ("http", "https")
            or parsed.scheme != scheme
            or not parsed.hostname
            or not authority.hostname
            or parsed.username is not None
            or parsed.password is not None
            or authority.username is not None
            or authority.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or "?" in origin
            or "#" in origin
            or parsed.netloc.endswith(":")
            or authority.netloc.endswith(":")
            or authority.path
            or authority.query
            or authority.fragment
        ):
            return False
        default_port = 443 if scheme == "https" else 80
        origin_port = parsed.port if parsed.port is not None else default_port
        host_port = authority.port if authority.port is not None else default_port
        return parsed.hostname.lower() == authority.hostname.lower() and origin_port == host_port
    except ValueError:
        return False


@ws_router.websocket("/labs/instances/{instance_id}/console/{node}/ws")
async def console_ws(websocket: WebSocket, instance_id: UUID, node: str) -> None:
    """Auth-gated WebSocket proxy to a Linux node's ttyd terminal.

    The HTTP-only tenant middleware does not run for the ws scope, so we resolve
    the tenant from the Host header (same resolver the app uses), the person from
    the session cookie (same logic as `require_web_user`), and run the shared
    `_authorize_console` gate BEFORE accepting. Any auth failure closes 1008.
    """
    if not _console_ws_same_origin(websocket):
        await websocket.close(code=1008)
        return
    host = (websocket.headers.get("host") or "").split(":")[0].lower()
    tenant = _tenant_resolver._resolve(host)
    if tenant is None:
        await websocket.close(code=1008)
        return

    runtime = resolve_database_runtime()
    db = runtime.session_factory()
    try:
        with runtime.tenant_scope(db, tenant.id):
            person = web_auth._current_person(db, tenant.id, websocket.cookies.get(web_auth.COOKIE))
            if person is None:
                await websocket.close(code=1008)
                return
            try:
                target = _authorize_console(db, tenant, person, instance_id, node)
            except HTTPException:
                await websocket.close(code=1008)
                return
            port = urlsplit(target).port
    finally:
        db.close()

    tenant_id = tenant.id

    async def _heartbeat() -> None:
        # In-process gate first: keystrokes cost a dict lookup, not a thread.
        if not lab_console_activity.try_acquire(instance_id):
            return
        # Background task: input forwarding must never wait on the database
        # (a saturated pool would otherwise stall keystrokes for the pool
        # timeout). The set keeps a reference so the task is not collected.
        task = asyncio.create_task(asyncio.to_thread(_write_console_heartbeat, tenant_id, instance_id))
        _heartbeat_tasks.add(task)
        task.add_done_callback(_heartbeat_tasks.discard)

    websocket.state.console_heartbeat = _heartbeat

    upstream_url = f"ws://{settings.lab_console_host}:{port}/labs/instances/{instance_id}/console/{node}/ws"
    await _proxy_ws(websocket, upstream_url)
