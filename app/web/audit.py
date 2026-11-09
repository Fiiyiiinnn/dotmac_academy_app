"""Admin audit-log viewer — GET /admin/audit.

Admin-only (require_web_role("admin") role gate only). No platform-admin token required —
that is distinct from /admin/settings which also has a token gate.

IMPORTANT: no db.commit() inside the handler — get_db owns the transaction and
commits after the response (a mid-handler commit clears the RLS tenant GUC).
"""

# Ported at new-composition genesis (web layer wave).

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_tenant
from app.services.api_identity_reads import count_audit_events, existing_people
from app.services.audit import list_events
from app.services.web_auth import require_web_role
from app.web.pagination import pagination_context
from app.web.templating import templates

router = APIRouter(
    prefix="/admin",
    dependencies=[Depends(require_tenant), Depends(require_web_role("admin"))],
)


@router.get("/audit", response_class=HTMLResponse)
def audit_log(
    request: Request,
    db: Session = Depends(get_db),
    action: str | None = Query(None),
    actor_email: str | None = Query(None),
    limit: int = Query(10, ge=1, le=50),
    offset: int = Query(0, ge=0),
) -> HTMLResponse:
    """Render the admin audit-log viewer with optional action/actor filters."""
    tenant = require_tenant(request)

    # Resolve actor email → person_id for filtering (user-friendly filter).
    filter_actor_id: UUID | None = None
    actor_not_found = False
    if actor_email:
        people = existing_people(db, tenant_id=tenant.id, email=actor_email)
        person = people[0] if people else None
        if person is not None:
            filter_actor_id = person.id
        else:
            actor_not_found = True

    events = (
        []
        if actor_not_found
        else list_events(
            db,
            tenant_id=tenant.id,
            limit=limit,
            offset=offset,
            action=action or None,
            actor_person_id=filter_actor_id,
        )
    )
    total = (
        0
        if actor_not_found
        else count_audit_events(db, tenant_id=tenant.id, action=action or None, actor_party_id=filter_actor_id)
    )

    # Current email is optional enrichment, never the stored forensic actor.
    person_ids = {e.actor_party_id for e in events if e.actor_party_id is not None}
    actor_map: dict[UUID, str] = {}
    if person_ids:
        people = existing_people(db, tenant_id=tenant.id, person_ids=person_ids)
        actor_map = {p.id: p.email for p in people}

    return templates.TemplateResponse(
        request,
        "admin/audit.html",
        {
            "request": request,
            "events": events,
            "actor_map": actor_map,
            "action_filter": action or "",
            "actor_email_filter": actor_email or "",
            "pagination": pagination_context(total=total, limit=limit, offset=offset),
        },
    )
