# Ported at new-composition genesis (web layer wave).
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import cast
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.engine import Row
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_tenant
from app.models.lab import LabInstance
from app.services.api_identity_reads import person_contacts
from app.services.assessment import override_score
from app.services.product_admin_access import require_lab_admin_access
from app.web.responses import hx_redirect
from app.web.templating import templates

router = APIRouter(
    prefix="/instructor/labs",
    dependencies=[Depends(require_tenant), Depends(require_lab_admin_access)],
)

# Statuses considered "live" for the monitor view.
_ACTIVE_STATUSES = ("active", "provisioning", "resetting", "queued")


@router.get("", response_class=HTMLResponse)
def monitor(request: Request, db: Session = Depends(get_db)):
    """Cross-person monitor of live lab instances for the current tenant."""
    tenant = require_tenant(request)
    rows: Sequence[Row[tuple[UUID, str, str, UUID, datetime | None, UUID]]] | list[tuple[object, ...]] = db.execute(
        select(
            LabInstance.id,
            LabInstance.status,
            LabInstance.instance_name,
            LabInstance.activity_id,
            LabInstance.last_active_at,
            LabInstance.person_id,
        )
        .where(LabInstance.tenant_id == tenant.id)
        .where(LabInstance.status.in_(_ACTIVE_STATUSES))
        .order_by(LabInstance.last_active_at.desc().nullslast())
    ).all()
    contacts = person_contacts(
        db,
        tenant_id=tenant.id,
        person_ids=[
            row.person_id for row in cast("Sequence[Row[tuple[UUID, str, str, UUID, datetime | None, UUID]]]", rows)
        ],
    )
    rows = [
        (*row[:5], contacts[row.person_id].email)
        for row in cast("Sequence[Row[tuple[UUID, str, str, UUID, datetime | None, UUID]]]", rows)
        if row.person_id in contacts
    ]
    return templates.TemplateResponse(request, "labs/admin_monitor.html", {"request": request, "rows": rows})


@router.post("/scores/{submission_id}/override", name="lab_override")
def override(
    submission_id: UUID,
    request: Request,
    score_value: float = Form(...),
    max_score: float = Form(...),
    reason: str = Form(...),
    db: Session = Depends(get_db),
):
    """Manually override the score for a lab submission (tenant-scoped)."""
    tenant = require_tenant(request)
    override_score(
        db,
        tenant_id=tenant.id,
        submission_id=submission_id,
        score_value=score_value,
        max_score=max_score,
        reason=reason,
    )
    # No db.commit() here — get_db commits after the response is returned.
    return hx_redirect(request, "/instructor/labs")
