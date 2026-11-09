"""Learner To-Do page — renders the unified timeline projection (P1 item 7).

Thin adapter: ``services.todo.timeline_for_person`` owns every derivation;
this router renders it and manages the calendar-feed token (P1 item 10).
"""

# Ported at new-composition genesis (web layer wave).

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_tenant
from app.services import ical as ical_service
from app.services import todo as todo_service
from app.services.identity import AcademyCurrentAccount
from app.services.web_auth import require_web_user
from app.web.templating import templates

router = APIRouter(dependencies=[Depends(require_tenant)])


def _context(request: Request, db: Session, person: AcademyCurrentAccount, *, feed_token: str | None) -> dict:
    tenant = require_tenant(request)
    return {
        "timeline": todo_service.timeline_for_person(db, tenant_id=tenant.id, person_id=person.id),
        "has_feed_token": ical_service.has_feed_token(db, tenant_id=tenant.id, person_id=person.id),
        "new_feed_token": feed_token,
    }


@router.get("/todo", response_class=HTMLResponse)
def todo_page(
    request: Request,
    person: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(request, "todo/index.html", _context(request, db, person, feed_token=None))


@router.post("/todo/calendar-feed", response_class=HTMLResponse)
def rotate_feed_token(
    request: Request,
    person: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
):
    """Mint (or rotate) the iCal feed token; the raw value is shown exactly once."""
    tenant = require_tenant(request)
    raw = ical_service.mint_feed_token(db, tenant_id=tenant.id, person_id=person.id)
    response = templates.TemplateResponse(request, "todo/index.html", _context(request, db, person, feed_token=raw))
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response
