"""Learner timetable — the student's own session schedule + session detail.

Thin adapter over ``services.scheduling``: lists the sessions of the person's
active cohorts (upcoming and past) and shows one session in detail with the
Join action when the join window is open. Also the target of agenda session
links (the old fallback pointed at a nonexistent /timetable).
"""

# Ported at new-composition genesis (web layer wave).

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_tenant
from app.services import scheduling
from app.services.exceptions import NotFoundError
from app.services.identity import AcademyCurrentAccount
from app.services.settings_store import effective
from app.services.web_auth import require_web_user
from app.web.templating import templates

router = APIRouter(dependencies=[Depends(require_tenant)])


@router.get("/timetable", response_class=HTMLResponse)
def learner_timetable(
    request: Request,
    person: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    tenant = require_tenant(request)
    now = datetime.now(UTC)
    sessions = scheduling.list_for_person(db, tenant_id=tenant.id, person_id=person.id, now=now)
    lead_minutes = cast(int, effective(db, require_store=True)["learning_join_lead_minutes"])
    joinable = {s.id for s, _ in sessions["upcoming"] if scheduling.join_is_open(s, now=now, lead_minutes=lead_minutes)}
    return templates.TemplateResponse(
        request,
        "learn/timetable.html",
        {"sessions": sessions, "joinable": joinable},
    )


@router.get("/timetable/sessions/{session_id}", response_class=HTMLResponse)
def learner_session_detail(
    session_id: UUID,
    request: Request,
    person: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    tenant = require_tenant(request)
    try:
        session, cohort_name = scheduling.get_session_for_person(
            db, tenant_id=tenant.id, person_id=person.id, session_id=session_id
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404) from exc
    now = datetime.now(UTC)
    lead_minutes = cast(int, effective(db, require_store=True)["learning_join_lead_minutes"])
    return templates.TemplateResponse(
        request,
        "learn/session_detail.html",
        {
            "s": session,
            "cohort_name": cohort_name,
            "join_open": scheduling.join_is_open(session, now=now, lead_minutes=lead_minutes),
        },
    )
