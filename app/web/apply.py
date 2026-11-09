"""Public application + entrance-assessment pages — the fibre-academy intake.

``GET/POST /apply`` records the application; if the chosen cohort has an entrance
assessment configured, the applicant is handed a tokenised link to a self-serve,
one-attempt exam whose result (a competency profile) is stored on the applicant.
Public (no login): tenant resolved from host, RLS primed by ``get_db``; htmx CSRF
via the same cookie→header shim as login.
"""

# Ported at new-composition genesis (web layer wave).

from __future__ import annotations

import html
from datetime import date
from uuid import UUID

from dotmac_learning.participation_models import Cohort, CohortTrack, Track
from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_tenant
from app.models.admissions import Applicant
from app.services import admissions as admissions_service
from app.services import applicant_email, entrance_exam
from app.services import erp_applicant_assessments as erp_registration
from app.services.entrance_exam import academy_default_bank_id
from app.services.exceptions import BadRequestError
from app.web.templating import templates

router = APIRouter(dependencies=[Depends(require_tenant)])

_THANKS = (
    '<div id="apply-result" class="mt-8 rounded-lg border border-brand-200 bg-brand-50 p-6">'
    '<h2 class="font-display text-xl font-[560] text-ink">Application received</h2>'
    '<p class="mt-2 text-sm text-ink-soft">Thanks, {name} — we\'ve got your application '
    "to the Dotmac Academy and will reach out by email.</p></div>"
)

_START_EXAM = (
    '<div id="apply-result" class="mt-8 rounded-lg border border-brand-200 bg-brand-50 p-6">'
    '<h2 class="font-display text-xl font-[560] text-ink">Application received — one more step</h2>'
    '<p class="mt-2 text-sm text-ink-soft">Thanks, {name}. Please complete a short entrance '
    "assessment so we can understand your level.</p>"
    '<a class="btn-primary mt-4 inline-block px-4 py-2 text-sm" href="/apply/assessment?token={token}">'
    "Start the assessment</a></div>"
)

_RESULT = (
    '<div id="exam-result" class="rounded-lg border border-brand-200 bg-brand-50 p-6">'
    '<h2 class="font-display text-xl font-[560] text-ink">{title}</h2>'
    '<p class="mt-2 text-sm text-ink-soft">{body}</p></div>'
)


def _open_track_choices(db: Session, tenant_id: UUID) -> list[dict[str, object]]:
    """Canonical active cohort/track pairs available for public intake."""
    stmt = (
        select(Cohort, Track)
        .join(
            CohortTrack,
            (CohortTrack.tenant_id == Cohort.tenant_id) & (CohortTrack.cohort_id == Cohort.id),
        )
        .join(
            Track,
            (Track.tenant_id == CohortTrack.tenant_id) & (Track.id == CohortTrack.track_id),
        )
        .where(Cohort.tenant_id == tenant_id)
        .where(Cohort.status == "active")
        .where(CohortTrack.status == "active")
        .where(Track.status == "active")
    )
    if academy_default_bank_id(db, tenant_id=tenant_id) is None:
        stmt = stmt.where(Cohort.entrance_bank_id.isnot(None))
    rows = db.execute(stmt.order_by(Track.name, Cohort.name)).all()
    return [
        {
            "value": f"{cohort.id}:{track.id}",
            "label": track.name,
            "cohort": cohort.name,
        }
        for cohort, track in rows
    ]


def _exam_questions(db: Session, tenant_id: UUID, applicant: Applicant) -> list:
    """Server-side sampled facts (count/autosave ids only; never rendered).

    The same deterministic subset of the pinned bank version that the
    grader reproduces, so what the candidate sits is what gets scored.
    Candidate rendering goes through ``entrance_sitting_view``.
    """
    facts, _version_id, _state = entrance_exam.sampled_facts(db, tenant_id=tenant_id, applicant=applicant)
    return facts


def _notice(request: Request, title: str, body: str) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "apply_assessment.html",
        {"request": request, "notice": {"title": title, "body": body}, "questions": None},
    )


def _deadline_closed_notice(request: Request) -> HTMLResponse:
    return _notice(
        request,
        "This assessment has closed",
        "The deadline for your entrance assessment has passed. If you were unable to "
        "sit it in time, contact us and we can reopen it for you.",
    )


@router.get("/apply")
def apply_form(request: Request, db: Session = Depends(get_db)):
    tenant = require_tenant(request)
    return templates.TemplateResponse(
        request,
        "apply.html",
        {"request": request, "track_choices": _open_track_choices(db, tenant.id)},
    )


def _d(v: str) -> date | None:
    try:
        return date.fromisoformat(v) if v else None
    except ValueError:
        return None


def _i(v: str) -> int | None:
    try:
        return int(v) if v.strip() != "" else None
    except ValueError:
        return None


def _b(v: str) -> bool | None:
    return {"yes": True, "no": False}.get((v or "").strip().lower())


@router.post("/apply")
def submit_apply(
    request: Request,
    first_name: str = Form(...),
    last_name: str = Form(...),
    email: str = Form(...),
    phone: str = Form(default=""),
    track_choice: str = Form(...),
    # --- evaluable profile ---
    date_of_birth: str = Form(default=""),
    state: str = Form(default=""),
    city: str = Form(default=""),
    highest_qualification: str = Form(default=""),
    field_of_study: str = Form(default=""),
    years_experience: str = Form(default=""),
    current_role: str = Form(default=""),
    has_device: str = Form(default=""),
    has_internet: str = Form(default=""),
    can_work_at_height: str = Form(default=""),
    available_from: str = Form(default=""),
    heard_from: str = Form(default=""),
    cv_url: str = Form(default=""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    tenant = require_tenant(request)
    try:
        cohort_raw, track_raw = track_choice.split(":", 1)
        cid, track_id = UUID(cohort_raw), UUID(track_raw)
    except (ValueError, AttributeError):
        raise BadRequestError("Choose an available training track.") from None
    applicant = admissions_service.submit_application(
        db,
        tenant_id=tenant.id,
        email=email,
        first_name=first_name,
        last_name=last_name,
        phone=phone or None,
        cohort_id=cid,
        track_id=track_id,
        source="website",
        profile={
            "date_of_birth": _d(date_of_birth),
            "state": state.strip() or None,
            "city": city.strip() or None,
            "highest_qualification": highest_qualification.strip() or None,
            "field_of_study": field_of_study.strip() or None,
            "years_experience": _i(years_experience),
            "current_role": current_role.strip() or None,
            "has_device": _b(has_device),
            "has_internet": _b(has_internet),
            "can_work_at_height": _b(can_work_at_height),
            "available_from": _d(available_from),
            "heard_from": heard_from.strip() or None,
            "cv_url": cv_url.strip() or None,
        },
    )
    safe_name = html.escape((first_name or "").strip()[:80]) or "there"

    applicant_email.send_application_received(db, applicant=applicant)

    if applicant.assessment_taken_at is None and entrance_exam.has_entrance_exam(db, applicant=applicant):
        # Mint the link, set the deadline, and EMAIL it. The link is still shown
        # on-screen so they can start now — but the email is the durable copy, so
        # closing the tab no longer costs them the exam.
        base = str(request.base_url).rstrip("/")
        inv = entrance_exam.invite(db, applicant=applicant, base_url=base)
        return HTMLResponse(_START_EXAM.format(name=safe_name, token=html.escape(inv["token"], quote=True)))
    return HTMLResponse(_THANKS.format(name=safe_name))


@router.get("/apply/assessment", response_class=HTMLResponse)
def assessment_page(request: Request, token: str = "", db: Session = Depends(get_db)):
    tenant = require_tenant(request)
    applicant = entrance_exam.applicant_for_token(db, tenant_id=tenant.id, raw=token)
    if applicant is None:
        return _notice(request, "Link not valid", "This assessment link is invalid or has expired.")
    if applicant.assessment_taken_at is not None:
        return _notice(request, "Already completed", "You've already completed the entrance assessment. Thank you.")
    if entrance_exam.past_deadline(applicant):
        return _deadline_closed_notice(request)
    if applicant.assessment_started_at is None:
        # Instruction screen first: the clock must start on an explicit choice,
        # never on a curious click of the emailed link.
        return templates.TemplateResponse(
            request,
            "apply_assessment.html",
            {
                "request": request,
                "notice": None,
                "questions": None,
                "intro": {
                    "minutes": entrance_exam.time_limit_minutes(db, applicant=applicant),
                    "question_count": len(_exam_questions(db, tenant.id, applicant)),
                },
                "token": token,
            },
        )
    timing = entrance_exam.timing_info(db, applicant=applicant)
    if timing["expired"]:
        return _notice(
            request,
            "Time is up",
            "Your entrance-assessment time has expired. If you were cut off before you "
            "could finish, contact us and we can reopen your sitting.",
        )
    return templates.TemplateResponse(
        request,
        "apply_assessment.html",
        {
            "request": request,
            "token": token,
            # Key-free Learning sitting projection (options only).
            "questions": entrance_exam.entrance_sitting_view(db, tenant_id=tenant.id, applicant=applicant),
            # Autosaved progress, so a resumed sitting comes back with answers intact.
            "saved": applicant.assessment_answers or {},
            "elapsed_seconds": timing["elapsed_seconds"],
            "remaining_seconds": timing["remaining_seconds"],
            "notice": None,
        },
    )


@router.post("/apply/assessment/start")
def assessment_start(request: Request, token: str = Form(...), db: Session = Depends(get_db)):
    """The explicit start: stamps the sitting and hands over to the questions."""
    tenant = require_tenant(request)
    applicant = entrance_exam.applicant_for_token(db, tenant_id=tenant.id, raw=token, lock=True)
    if applicant is None:
        return _notice(request, "Link not valid", "This assessment link is invalid or has expired.")
    if applicant.assessment_taken_at is not None:
        return _notice(request, "Already completed", "You've already completed the entrance assessment. Thank you.")
    if entrance_exam.past_deadline(applicant):
        return _deadline_closed_notice(request)
    entrance_exam.start_exam(db, applicant=applicant)
    url = f"/apply/assessment?token={token}"
    if request.headers.get("HX-Request"):
        return Response(status_code=204, headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)


@router.post("/apply/assessment/save")
async def assessment_autosave(request: Request, db: Session = Depends(get_db)):
    """Autosave in-progress answers (fire-and-forget from the exam page).

    This is what makes a dropped connection survivable: without it the candidate
    loses every answer while the clock keeps running. Always 204 — a failed
    autosave must never interrupt the sitting.
    """
    tenant = require_tenant(request)
    form = await request.form()
    applicant = entrance_exam.applicant_for_token(
        db,
        tenant_id=tenant.id,
        raw=str(form.get("token") or ""),
        lock=True,
    )
    if applicant is None or applicant.assessment_taken_at is not None or entrance_exam.past_deadline(applicant):
        return Response(status_code=204)
    questions = _exam_questions(db, tenant.id, applicant)
    answers = {q.ext_id: form.getlist(q.ext_id) for q in questions}
    entrance_exam.record_elapsed(db, applicant=applicant)
    entrance_exam.save_answers(db, applicant=applicant, answers=answers)
    return Response(status_code=204)


@router.post("/apply/assessment", response_class=HTMLResponse)
async def assessment_submit(request: Request, token: str = Form(...), db: Session = Depends(get_db)):
    from app.admission_assessment_context import AssessmentGradeRejected, submit_applicant_assessment

    try:
        submission = await submit_applicant_assessment(db, request=request)
    except AssessmentGradeRejected:
        return HTMLResponse(
            _RESULT.format(title="Could not submit", body="Something went wrong recording your assessment.")
        )
    if submission.disposition == "already_completed":
        return _notice(request, "Already completed", "This assessment was already submitted, or the link is invalid.")
    if submission.disposition == "deadline_closed":
        return _deadline_closed_notice(request)
    applicant = submission.applicant
    if applicant.assessment_return_url:
        try:
            return_url = erp_registration.validate_return_url(applicant.assessment_return_url)
        except erp_registration.RegistrationError:
            return_url = None
        if return_url is not None:
            if request.headers.get("HX-Request"):
                return Response(status_code=204, headers={"HX-Redirect": return_url})
            return RedirectResponse(return_url, status_code=303)
    return HTMLResponse(
        _RESULT.format(
            title="Assessment submitted",
            body="Thank you — your entrance assessment has been recorded. Watch your email for the next step.",
        )
    )
