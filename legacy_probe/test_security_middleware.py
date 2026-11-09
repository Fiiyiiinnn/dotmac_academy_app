"""Academy's contract with the kernel-owned middleware stack."""

from __future__ import annotations

import asyncio
from http import cookies
from typing import Any
from uuid import uuid4

from dotmac_kernel.middleware.csrf import CSRF_COOKIE, CSRFMiddleware, require_csrf
from dotmac_kernel.middleware.observability import ObservabilityMiddleware
from dotmac_kernel.middleware.rate_limit import RateLimitMiddleware
from dotmac_kernel.middleware.security_headers import SecurityHeadersMiddleware
from dotmac_kernel.middleware.tenant import TenantResolverMiddleware
from dotmac_kernel.models import Tenant
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.assembly import assembly


def _csrf_client() -> TestClient:
    """Minimal app whose route carries the kernel's ``require_csrf`` dependency.

    The middleware only issues a cookie on a safe request to a route carrying
    ``require_csrf``, and validation happens only in that dependency.
    """
    app = FastAPI()

    @app.get("/form", dependencies=[Depends(require_csrf)])
    def form() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/form", dependencies=[Depends(require_csrf)])
    def submit() -> dict[str, bool]:
        return {"ok": True}

    # Build the middleware from the product's own configuration (secret,
    # session cookie names, lifetime), never from a hand copy. Kwargs are not printed.
    from app.main import app as product

    spec = next(m for m in product.user_middleware if m.cls is CSRFMiddleware)
    app.add_middleware(CSRFMiddleware, **spec.kwargs)
    return TestClient(app, base_url="http://testserver")


def test_csrf_sets_cookie_on_safe_request_and_blocks_cookie_post_without_header():
    client = _csrf_client()

    get_response = client.get("/form")
    assert get_response.status_code == 200
    issued = client.cookies.get(CSRF_COOKIE)
    assert issued

    # The cookie alone (no header or form field) is not proof.
    post_response = client.post("/form")
    assert post_response.status_code == 403


def test_csrf_allows_matching_double_submit_token():
    client = _csrf_client()
    assert client.get("/form").status_code == 200
    issued = client.cookies.get(CSRF_COOKIE)
    assert issued

    ok = client.post("/form", headers={"x-csrf-token": issued})
    assert ok.status_code == 200

    # A self-chosen value is refused even when cookie and header agree.
    forged = TestClient(client.app, base_url="http://testserver")
    forged.cookies.set(CSRF_COOKIE, "known-token")
    refused = forged.post("/form", headers={"x-csrf-token": "known-token"})
    assert refused.status_code == 403

    # A genuine token paired with a different header value is refused.
    mismatch = client.post("/form", headers={"x-csrf-token": issued + "x"})
    assert mismatch.status_code == 403


def test_csrf_token_is_bound_to_the_session_cookie():
    client = _csrf_client()
    client.cookies.set("session", "session-one")
    assert client.get("/form").status_code == 200
    issued = client.cookies.get(CSRF_COOKIE)
    assert issued
    assert client.post("/form", headers={"x-csrf-token": issued}).status_code == 200
    # The same token under a different session is refused.
    client.cookies.set("session", "session-two")
    assert client.post("/form", headers={"x-csrf-token": issued}).status_code == 403


def test_rate_limit_key_isolated_by_path():
    app = RateLimitMiddleware(_ok_app, requests=1, window_seconds=60)

    assert _run(app, method="GET", path="/a")["status"] == 200
    assert _run(app, method="GET", path="/a")["status"] == 429
    assert _run(app, method="GET", path="/b")["status"] == 200


def test_rate_limit_key_isolated_by_tenant():
    app = RateLimitMiddleware(_ok_app, requests=1, window_seconds=60)
    tenant_a = _Tenant(str(uuid4()))
    tenant_b = _Tenant(str(uuid4()))

    assert _run(app, method="GET", path="/foo", tenant=tenant_a)["status"] == 200
    assert _run(app, method="GET", path="/foo", tenant=tenant_a)["status"] == 429
    assert _run(app, method="GET", path="/foo", tenant=tenant_b)["status"] == 200


def test_observability_generates_request_id_by_default():
    app = ObservabilityMiddleware(_ok_app)

    response = _run(
        app,
        method="GET",
        path="/ping",
        headers=[(b"x-request-id", b"untrusted-id")],
    )
    assert response["status"] == 200
    response_request_ids = [value for key, value in response["headers"] if key == b"x-request-id"]
    assert response_request_ids
    assert response_request_ids[0] != b"untrusted-id"


def test_observability_can_trust_inbound_request_id():
    app = ObservabilityMiddleware(_ok_app, trust_inbound_request_id=True)

    response = _run(
        app,
        method="GET",
        path="/ping",
        headers=[(b"x-request-id", b"req-123")],
    )
    assert response["status"] == 200
    assert (b"x-request-id", b"req-123") in response["headers"]


def test_security_headers_cover_browser_responses():
    policy = assembly.security_policy
    response = _run(
        SecurityHeadersMiddleware(
            _ok_app,
            content_security_policy=policy.content_security_policy,
            cross_origin_opener_policy=policy.cross_origin_opener_policy,
            cross_origin_resource_policy=policy.cross_origin_resource_policy,
        ),
        method="GET",
        path="/",
        scheme="https",
    )
    headers = dict(response["headers"])
    assert b"default-src 'self'" in headers[b"content-security-policy"]
    assert headers[b"x-content-type-options"] == b"nosniff"
    assert headers[b"x-frame-options"] == b"DENY"
    assert headers[b"referrer-policy"] == b"strict-origin-when-cross-origin"
    assert headers[b"strict-transport-security"] == b"max-age=63072000; includeSubDomains"
    assert headers[b"cross-origin-opener-policy"] == b"same-origin"
    assert headers[b"cross-origin-resource-policy"] == b"same-origin"


def test_single_academy_slug_rejects_other_tenant(monkeypatch):
    """The binding now comes from the kernel's startup assertion, not our config."""
    from dotmac_kernel.tenancy import bind_single_tenant, clear_single_tenant_binding

    bind_single_tenant("alpha")
    monkeypatch.setattr(
        "dotmac_kernel.middleware.tenant.single_tenant_binding",
        lambda: "alpha",
        raising=False,
    )
    try:
        _assert_rejects_other_tenant()
    finally:
        clear_single_tenant_binding()


def _assert_rejects_other_tenant() -> None:
    middleware = TenantResolverMiddleware(_ok_app)
    alpha = Tenant(slug="alpha", name="Alpha")
    beta = Tenant(slug="beta", name="Beta")

    assert middleware._allow(alpha) is alpha
    assert middleware._allow(beta) is None


async def _ok_app(scope, receive, send) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": b'{"ok":true}'})


def _run(
    app,
    *,
    method: str,
    path: str,
    headers: list[tuple[bytes, bytes]] | None = None,
    tenant: object | None = None,
    scheme: str = "http",
) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers or [],
        "scheme": scheme,
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
        "state": {"tenant": tenant} if tenant is not None else {},
    }
    asyncio.run(app(scope, receive, send))
    start = next(message for message in messages if message["type"] == "http.response.start")
    return {"status": start["status"], "headers": start["headers"], "messages": messages}


def _cookie_value(response: dict[str, Any], name: str) -> str | None:
    jar = cookies.SimpleCookie()
    for header, value in response["headers"]:
        if header == b"set-cookie":
            jar.load(value.decode())
    morsel = jar.get(name)
    return morsel.value if morsel is not None else None


class _Tenant:
    def __init__(self, tenant_id: str) -> None:
        self.id = tenant_id
