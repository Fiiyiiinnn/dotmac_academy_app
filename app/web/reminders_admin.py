"""Admin reminder history + authorized resend — GET/POST /admin/reminders.

Thin adapter over the reminder policy service: the page projects the
ReminderLog ledger joined with outbox delivery state; resend is an audited
service action. No decisions live here.

IMPORTANT: no db.commit() inside handlers — get_db owns the transaction
(a mid-handler commit clears the RLS tenant GUC).
"""

# Ported at new-composition genesis (web layer wave).

from __future__ import annotations

from uuid import UUID

from dotmac_kernel.models import Party, PartyPerson, PartyType
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_tenant
from app.models.email_outbox import EmailOutbox
from app.models.reminder import EVENT_KINDS, ReminderLog
from app.services import reminders as reminders_service
from app.services.identity import AcademyCurrentAccount
from app.services.product_admin_access import require_reminders_admin_access
from app.web.pagination import pagination_context
from app.web.templating import templates

router = APIRouter(
    prefix="/admin",
    dependencies=[Depends(require_tenant), Depends(require_reminders_admin_access)],
)


@router.get("/reminders", response_class=HTMLResponse)
def reminders_history(
    request: Request,
    db: Session = Depends(get_db),
    q: str | None = Query(None),
    delivery_status: str | None = Query(None, alias="status"),
    event_kind: str | None = Query(None),
    limit: int = Query(10, ge=1, le=50),
    offset: int = Query(0, ge=0),
) -> HTMLResponse:
    tenant = require_tenant(request)
    count_stmt = (
        select(func.count())
        .select_from(ReminderLog)
        .join(
            Party,
            (Party.id == ReminderLog.person_id) & (Party.tenant_id == ReminderLog.tenant_id),
        )
        .join(PartyPerson, PartyPerson.party_id == Party.id)
        .where(Party.party_type == PartyType.person)
        .where(ReminderLog.tenant_id == tenant.id)
    )
    if delivery_status:
        count_stmt = count_stmt.where(ReminderLog.status == delivery_status)
    if event_kind:
        count_stmt = count_stmt.where(ReminderLog.event_kind == event_kind)
    if q and (term := q.strip()):
        pattern = f"%{term}%"
        count_stmt = count_stmt.where(
            or_(
                Party.email.ilike(pattern),
                ReminderLog.title.ilike(pattern),
                ReminderLog.event_kind.ilike(pattern),
            )
        )
    total = int(db.scalar(count_stmt) or 0)
    rows = reminders_service.recent_log(
        db,
        tenant_id=tenant.id,
        limit=limit,
        offset=offset,
        status=delivery_status,
        event_kind=event_kind,
        search=q,
    )
    outbox_keys = [log.outbox_key for log, _person in rows if log.outbox_key]
    delivery: dict[str, EmailOutbox] = {}
    if outbox_keys:
        for outbox in db.scalars(
            select(EmailOutbox)
            .where(EmailOutbox.tenant_id == tenant.id)
            .where(EmailOutbox.idempotency_key.in_(outbox_keys))
        ).all():
            delivery[outbox.idempotency_key] = outbox

    def reminder_count(status_value: str | None = None) -> int:
        stmt = select(func.count()).select_from(ReminderLog).where(ReminderLog.tenant_id == tenant.id)
        if status_value:
            stmt = stmt.where(ReminderLog.status == status_value)
        return int(db.scalar(stmt) or 0)

    provider_errors = int(
        db.scalar(
            select(func.count())
            .select_from(EmailOutbox)
            .where(EmailOutbox.tenant_id == tenant.id)
            .where(EmailOutbox.kind.like("reminder_%"))
            .where(EmailOutbox.last_error.is_not(None))
        )
        or 0
    )
    return templates.TemplateResponse(
        request,
        "admin/reminders.html",
        {
            "request": request,
            "rows": rows,
            "delivery": delivery,
            "event_kinds": EVENT_KINDS,
            "search_query": q or "",
            "selected_status": delivery_status or "",
            "selected_event_kind": event_kind or "",
            "metrics": {
                "total": reminder_count(),
                "sent": reminder_count("sent"),
                "queued": reminder_count("queued"),
                "errors": provider_errors,
            },
            "pagination": pagination_context(total=total, limit=limit, offset=offset),
        },
    )


@router.post("/reminders/{log_id}/resend", response_class=HTMLResponse)
def reminders_resend(
    log_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    person: AcademyCurrentAccount = Depends(require_reminders_admin_access),
) -> HTMLResponse:
    tenant = require_tenant(request)
    try:
        log = reminders_service.resend(db, tenant_id=tenant.id, request=request, log_id=log_id)
    except ValueError as exc:
        return HTMLResponse(f'<span class="text-sm font-semibold text-red-700">{exc}</span>')
    return HTMLResponse(f'<span class="text-sm font-semibold text-brand-700">Requeued ({log.outbox_key})</span>')
