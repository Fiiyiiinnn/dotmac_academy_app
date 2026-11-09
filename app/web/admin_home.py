# Ported at new-composition genesis (web layer wave).
from __future__ import annotations

import dotmac_learning as learning
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_tenant
from app.models.lab import LabInstance
from app.services.api_identity_reads import count_all_people
from app.services.product_admin_access import require_admin_home_access
from app.web.templating import templates

router = APIRouter(
    prefix="/admin",
    dependencies=[Depends(require_tenant), Depends(require_admin_home_access)],
)


@router.get("", response_class=HTMLResponse)
def admin_console(request: Request, db: Session = Depends(get_db)):
    tenant = require_tenant(request)

    def _count(model, *extra):
        stmt = select(func.count()).select_from(model).where(model.tenant_id == tenant.id)
        for clause in extra:
            stmt = stmt.where(clause)
        return db.execute(stmt).scalar_one()

    stats = {
        "people": count_all_people(db, tenant_id=tenant.id),
        "cohorts": len(learning.instructor_cohort_choices(db, tenant_id=tenant.id)),
        "courses": len(learning.all_courses(db, tenant_id=tenant.id)),
        "labs": _count(
            LabInstance,
            LabInstance.status.in_(("active", "provisioning", "resetting")),
        ),
    }
    return templates.TemplateResponse(request, "admin/console.html", {"request": request, "stats": stats})
