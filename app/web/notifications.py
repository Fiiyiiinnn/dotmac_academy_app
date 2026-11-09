# Ported at new-composition genesis (web layer wave).
# app/web/notifications.py
"""Notifications center — /notifications."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_tenant
from app.services import notifications as notif_svc
from app.services import settings_store
from app.services.identity import AcademyCurrentAccount
from app.services.web_auth import require_web_user
from app.web.responses import hx_redirect
from app.web.templating import templates

router = APIRouter(dependencies=[Depends(require_tenant)])


@router.get("/notifications", response_class=HTMLResponse)
def notifications_list(
    request: Request,
    person: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    tenant = require_tenant(request)
    policy = settings_store.effective(db, require_store=True)
    items = notif_svc.recent(db, tenant_id=tenant.id, person_id=person.id, limit=policy.notifications_recent_limit)
    return templates.TemplateResponse(
        request,
        "notifications.html",
        {
            "request": request,
            "person": person,
            "notifications": items,
        },
    )


@router.post("/notifications/read-all")
def read_all(
    request: Request,
    person: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
) -> Response:
    tenant = require_tenant(request)
    notif_svc.mark_all_read(db, tenant_id=tenant.id, person_id=person.id)
    return hx_redirect(request, "/notifications")
