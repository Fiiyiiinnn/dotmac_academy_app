"""Public onboarding portal — the self-serve checklist an accepted applicant
works through to become an enrolled student.

``GET /onboarding?token=…`` shows the applicant's checklist (entrance
assessment, confirm details, orientation). Each POST completes its task; when
the checklist is done the applicant is auto-enrolled: Person + Enrollment +
``student`` role, plus a password-setup invite email if they have no login yet.

Public (no login): tenant resolved from host, token resolved to the applicant
the same way as the entrance-exam link. Forms post via htmx with the cookie→
header CSRF shim; each POST answers with a redirect back to the checklist.
"""

# Ported at new-composition genesis (web layer wave).

from __future__ import annotations

from typing import cast

from dotmac_learning.participation_models import Cohort
from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_tenant
from app.models.admissions import Applicant
from app.services import admissions as admissions_service
from app.services import applicant_email, entrance_exam, onboarding
from app.web.templating import templates

router = APIRouter(dependencies=[Depends(require_tenant)])


def _notice(request: Request, title: str, body: str) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "onboarding.html",
        {"request": request, "notice": {"title": title, "body": body}, "applicant": None},
    )


def _redirect(request: Request, token: str, flag: str | None = None) -> Response:
    """Back to the checklist. htmx posts get HX-Redirect (client-side GET) so the
    whole page re-renders; plain posts get a classic 303."""
    url = f"/onboarding?token={token}"
    if flag:
        url += f"&{flag}=1"
    if request.headers.get("HX-Request"):
        return Response(status_code=204, headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)


def _resolve(request: Request, db: Session, token: str) -> Applicant | None:
    tenant = require_tenant(request)
    return admissions_service.applicant_for_onboarding_token(db, tenant_id=tenant.id, raw=token)


def _finish_task_and_maybe_enroll(request: Request, db: Session, token: str, key: str):
    from app.services.invitation_issuance import finish_onboarding_step

    with finish_onboarding_step(db, tenant_id=require_tenant(request).id, raw=token, key=key) as result:
        if result is None:
            return _notice(request, "Link not valid", "This onboarding link is invalid or has expired.")
        if result.enrolled_now:
            base = str(request.base_url).rstrip("/")
            setup_url = f"{base}/accept-invite?token={result.invite_raw}" if result.invite_raw else None
            applicant_email.send_enrollment_welcome(
                db, applicant=cast(Applicant, result.applicant), setup_url=setup_url
            )
    return _redirect(request, token)


@router.get("/onboarding")
def onboarding_page(request: Request, token: str = "", resent: str = "", db: Session = Depends(get_db)):
    applicant = _resolve(request, db, token)
    if applicant is None:
        return _notice(request, "Link not valid", "This onboarding link is invalid or has expired.")
    tasks = onboarding.list_tasks(db, tenant_id=applicant.tenant_id, applicant_id=applicant.id)
    cohort = db.get(Cohort, applicant.cohort_id) if applicant.cohort_id else None
    return templates.TemplateResponse(
        request,
        "onboarding.html",
        {
            "request": request,
            "notice": None,
            "applicant": applicant,
            "tasks": {t.key: t.status for t in tasks},
            "cohort": cohort,
            "token": token,
            "resent": bool(resent),
        },
    )


@router.post("/onboarding/confirm")
def confirm_details(request: Request, token: str = Form(...), db: Session = Depends(get_db)):
    return _finish_task_and_maybe_enroll(request, db, token, "confirm_details")


@router.post("/onboarding/orientation")
def acknowledge_orientation(request: Request, token: str = Form(...), db: Session = Depends(get_db)):
    return _finish_task_and_maybe_enroll(request, db, token, "orientation")


@router.post("/onboarding/resend-assessment")
def resend_assessment(request: Request, token: str = Form(...), db: Session = Depends(get_db)):
    """Re-mint and email the entrance-exam link (its raw token is never shown
    here — the applicant may have lost the original email)."""
    applicant = _resolve(request, db, token)
    if applicant is None:
        return _notice(request, "Link not valid", "This onboarding link is invalid or has expired.")
    if applicant.assessment_taken_at is None and entrance_exam.has_entrance_exam(db, applicant=applicant):
        base = str(request.base_url).rstrip("/")
        entrance_exam.invite(db, applicant=applicant, base_url=base)
    return _redirect(request, token, flag="resent")
