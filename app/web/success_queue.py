"""Success Queue + segment messaging — instructor/admin surface (roadmap P3b).

Thin adapter over ``services.success_queue``: the page projects entries with
their reason, facts, freshness and recommended action; transitions and the
message-these-learners action are audited service calls.

IMPORTANT: no db.commit() inside handlers — get_db owns the transaction
(a mid-handler commit clears the RLS tenant GUC).
"""

# Ported at new-composition genesis (web layer wave).

from __future__ import annotations

from uuid import UUID

from dotmac_learning import SuccessPersonFact
from dotmac_learning.participation_models import Cohort
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_tenant
from app.services import success_queue as queue_service
from app.services.exceptions import BadRequestError, NotFoundError
from app.services.identity import AcademyCurrentAccount
from app.services.roles import role_slugs
from app.services.web_auth import require_web_user
from app.web.templating import templates

router = APIRouter(dependencies=[Depends(require_tenant)])

_SEGMENT_LABELS = {
    "inactive": "No activity recently",
    "overdue_work": "Assignment overdue",
    "below_passing": "Below passing grade",
    "failed_final": "Failed final attempt",
    "almost_complete": "Course almost complete",
}


def _require_staff(db: Session, tenant_id: UUID, person_id: UUID) -> None:
    if not {"instructor", "admin"} & role_slugs(db, tenant_id, person_id):
        raise HTTPException(status_code=403, detail="Instructor or admin role required")


@router.get("/success-queue", response_class=HTMLResponse)
def queue_index(
    request: Request,
    person: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
):
    tenant = require_tenant(request)
    _require_staff(db, tenant.id, person.id)
    status = request.query_params.get("status") or "open"
    severity = request.query_params.get("severity") or None
    cohort_raw = request.query_params.get("cohort") or None
    cohort_id = UUID(cohort_raw) if cohort_raw else None
    entries = queue_service.list_entries(
        db,
        tenant_id=tenant.id,
        status=None if status == "all" else status,
        severity=severity,
        cohort_id=cohort_id,
    )
    cohorts = db.scalars(select(Cohort).where(Cohort.tenant_id == tenant.id)).all()
    return templates.TemplateResponse(
        request,
        "instructor/success_queue.html",
        {
            "entries": entries,
            "cohorts": cohorts,
            "status": status,
            "severity": severity or "",
            "cohort_id": str(cohort_id) if cohort_id else "",
            "segments": _SEGMENT_LABELS,
        },
    )


@router.post("/success-queue/{entry_id}/{action}", response_class=HTMLResponse)
def queue_action(
    entry_id: UUID,
    action: str,
    request: Request,
    person: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
):
    tenant = require_tenant(request)
    _require_staff(db, tenant.id, person.id)
    if action not in {"acknowledge", "assign", "resolve"}:
        raise HTTPException(status_code=404)
    try:
        entry = queue_service.transition(db, tenant_id=tenant.id, entry_id=entry_id, action=action, request=request)
    except NotFoundError:
        raise HTTPException(status_code=404) from None
    except BadRequestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return HTMLResponse(
        f'<span class="rounded-full bg-brand-50 px-3 py-1.5 text-xs font-semibold text-brand-700">{entry.status}</span>'
    )


@router.get("/success-queue/message", response_class=HTMLResponse)
def message_form(
    request: Request,
    person: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
):
    tenant = require_tenant(request)
    _require_staff(db, tenant.id, person.id)
    cohort_raw = request.query_params.get("cohort") or ""
    segment = request.query_params.get("segment") or "inactive"
    if segment not in _SEGMENT_LABELS:
        raise HTTPException(status_code=404)
    cohorts = db.scalars(select(Cohort).where(Cohort.tenant_id == tenant.id)).all()
    members: list[SuccessPersonFact] = []
    cohort = None
    if cohort_raw:
        cohort = db.scalars(
            select(Cohort).where(Cohort.tenant_id == tenant.id).where(Cohort.id == UUID(cohort_raw))
        ).first()
        if cohort is None:
            raise HTTPException(status_code=404)
        members = queue_service.segment_members(db, tenant_id=tenant.id, cohort_id=cohort.id, segment=segment)
    return templates.TemplateResponse(
        request,
        "instructor/segment_message.html",
        {
            "cohorts": cohorts,
            "cohort": cohort,
            "segment": segment,
            "segments": _SEGMENT_LABELS,
            "members": members,
            "sent": None,
        },
    )


@router.post("/success-queue/message", response_class=HTMLResponse)
def message_send(
    request: Request,
    cohort_id: UUID = Form(...),
    segment: str = Form(...),
    subject: str = Form(...),
    body: str = Form(...),
    person: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
):
    tenant = require_tenant(request)
    _require_staff(db, tenant.id, person.id)
    if segment not in _SEGMENT_LABELS:
        raise HTTPException(status_code=404)
    cohort = db.scalars(select(Cohort).where(Cohort.tenant_id == tenant.id).where(Cohort.id == cohort_id)).first()
    if cohort is None:
        raise HTTPException(status_code=404)
    try:
        result = queue_service.message_segment(
            db,
            tenant_id=tenant.id,
            cohort_id=cohort_id,
            segment=segment,
            subject=subject,
            body=body,
            request=request,
        )
    except BadRequestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return HTMLResponse(
        f'<div class="rounded-lg border border-brand-200 bg-brand-50 p-4 text-sm '
        f'font-semibold text-brand-700">Sent to {result["recipients"]} learner(s) — '
        f"in-app now, email via the outbox.</div>"
    )
