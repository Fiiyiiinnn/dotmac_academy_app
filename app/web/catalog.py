# Ported at new-composition genesis (web layer wave).
# app/web/catalog.py
"""Course catalog web router — /courses, /courses/{slug}, and /calendar."""

from __future__ import annotations

import html
from itertools import groupby
from uuid import UUID

import dotmac_learning as learning
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_tenant
from app.services import announcements as ann_svc
from app.services import learning_events, management_inquiries
from app.services.agenda import upcoming_for_person
from app.services.catalog import (
    all_courses,
    catalog_course_by_slug,
    course_completion,
    course_structure,
    my_courses,
    public_catalog,
)
from app.services.entitlements import course_access_states, open_course_ids
from app.services.identity import AcademyCurrentAccount
from app.services.localtime import to_local
from app.services.roles import role_slugs
from app.services.settings_store import effective
from app.services.web_auth import optional_web_user, require_web_user
from app.web.templating import templates

router = APIRouter(dependencies=[Depends(require_tenant)])


def _is_staff(db: Session, tenant_id: UUID, person_id: UUID) -> bool:
    return bool(set(effective(db, require_store=True).catalog_staff_roles) & role_slugs(db, tenant_id, person_id))


@router.get("/siwes-ict-training", response_class=HTMLResponse)
def siwes_ict_training(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "public/siwes-ict-training.html", {})


@router.get("/nysc-ict-training", response_class=HTMLResponse)
def nysc_ict_training(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "public/nysc-ict-training.html", {})


@router.get("/fibre-optic-training", response_class=HTMLResponse)
def fibre_optic_training(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "public/fibre-optic-training.html", {})


@router.get("/courses", response_class=HTMLResponse)
def courses_list(
    request: Request,
    person: AcademyCurrentAccount | None = Depends(optional_web_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Signed-in: 'My courses' (+ 'All courses' for staff). Anonymous: the
    public catalog projection (ADR 0003 — ``Course.listed`` is the selector)."""
    tenant = require_tenant(request)
    if person is None:
        listed = public_catalog(db, tenant_id=tenant.id)
        mgmt = [i for i in listed if i["course"].discipline == "management"]
        tech = [i for i in listed if i["course"].discipline != "management"]
        return templates.TemplateResponse(
            request,
            "public/courses.html",
            {
                "courses": listed,
                "tech": tech,
                "mgmt": mgmt,
                "management_contact_email": effective(db).management_inquiry_recipient,
            },
        )
    staff = _is_staff(db, tenant.id, person.id)

    enrolled = my_courses(db, tenant_id=tenant.id, person_id=person.id)
    access_states = course_access_states(db, tenant_id=tenant.id, person_id=person.id)
    my: list[dict] = []
    for c in enrolled:
        state = access_states.get(c.id)
        my.append(
            {
                "course": c,
                "pct": course_completion(db, tenant_id=tenant.id, person_id=person.id, course_id=c.id),
                "locked": state.locked if state else False,
                "locked_reason": state.locked_reason if state else None,
            }
        )

    all_: list[learning.CourseFact] | None = None
    if staff:
        all_ = all_courses(db, tenant_id=tenant.id)

    return templates.TemplateResponse(
        request,
        "learn/courses.html",
        {
            "request": request,
            "person": person,
            "my_courses": my,
            "all_courses": all_,
            "is_staff": staff,
        },
    )


@router.get("/management-enrollment", response_class=HTMLResponse)
def management_enrollment_form(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    tenant = require_tenant(request)
    listed = public_catalog(db, tenant_id=tenant.id)
    mgmt = [i for i in listed if i["course"].discipline == "management"]
    return templates.TemplateResponse(
        request,
        "public/management_enrollment.html",
        {
            "request": request,
            "mgmt": mgmt,
            "management_contact_email": effective(db).management_inquiry_recipient,
        },
    )


@router.post("/management-enrollment", response_class=HTMLResponse)
def submit_management_enrollment(
    request: Request,
    first_name: str = Form(...),
    last_name: str = Form(...),
    email: str = Form(...),
    phone: str = Form(default=""),
    learner_type: str = Form(default=""),
    course_interest: str = Form(default=""),
    message: str = Form(default=""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    tenant = require_tenant(request)
    queued = management_inquiries.queue_management_inquiry(
        db,
        tenant_id=tenant.id,
        first_name=first_name,
        last_name=last_name,
        email=email,
        phone=phone,
        learner_type=learner_type,
        course_interest=course_interest,
        message=message,
    )
    result_id = 'id="management-enrollment-result"'
    if queued:
        result_class = 'class="mt-6 rounded-lg border border-brand-200 bg-brand-50 p-6"'
        return HTMLResponse(
            f'<div {result_id} {result_class} role="status">'
            '<h2 class="font-display text-xl font-[560] text-ink">Inquiry received</h2>'
            '<p class="mt-2 text-sm text-ink-soft">'
            "Thanks. We will review your management-course request and reply by email."
            "</p>"
            "</div>"
        )
    contact_email = html.escape(str(effective(db).management_inquiry_recipient), quote=True)
    result_class = 'class="mt-6 rounded-lg border border-clay-500/30 bg-clay-500/10 p-6"'
    return HTMLResponse(
        f'<div {result_id} {result_class} role="status">'
        '<h2 class="font-display text-xl font-[560] text-ink">Inquiry not sent</h2>'
        f'<p class="mt-2 text-sm text-ink-soft">Please email {contact_email} and we will help you enroll.</p>'
        "</div>"
    )


@router.get("/courses/{slug}", response_class=HTMLResponse)
def course_landing(
    slug: str,
    request: Request,
    person: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Course landing page — Part-grouped structure + Continue CTA.

    Non-enrolled students receive 403; unknown slug 404; staff bypass the
    entitlement check so they can preview any course.
    """
    tenant = require_tenant(request)
    course = catalog_course_by_slug(db, tenant_id=tenant.id, slug=slug)
    if course is None:
        raise HTTPException(status_code=404)

    staff = _is_staff(db, tenant.id, person.id)
    if not staff:
        # 403 for non-enrolled students and closed windows — but an unmet
        # prerequisite still gets the landing page: course_structure marks it
        # locked and the template shows the banner (content routes still 403).
        if course.id not in open_course_ids(db, tenant_id=tenant.id, person_id=person.id):
            raise HTTPException(status_code=403)

    learning_events.emit(
        db,
        tenant_id=tenant.id,
        person_id=person.id,
        kind="course_viewed",
        course_id=course.id,
        subject_id=course.id,
    )
    structure = course_structure(db, tenant_id=tenant.id, person_id=person.id, course=course)
    pct = course_completion(db, tenant_id=tenant.id, person_id=person.id, course_id=course.id)

    return templates.TemplateResponse(
        request,
        "learn/course.html",
        {
            "request": request,
            "person": person,
            "course": course,
            "structure": structure,
            "pct": pct,
            "is_staff": staff,
        },
    )


@router.get("/announcements", response_class=HTMLResponse)
def announcements(
    request: Request,
    person: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Learner announcements list — tenant-wide and cohort-targeted."""
    tenant = require_tenant(request)
    items = ann_svc.for_person(db, tenant_id=tenant.id, person_id=person.id)
    cohort_ids = {a.cohort_id for a in items if a.cohort_id is not None}
    cohort_map: dict = {}
    if cohort_ids:
        cohorts = learning.instructor_cohort_choices(db, tenant_id=tenant.id)
        cohort_map = {c.id: c.name for c in cohorts if c.id in cohort_ids}
    return templates.TemplateResponse(
        request,
        "learn/announcements.html",
        {"request": request, "person": person, "announcements": items, "cohort_map": cohort_map},
    )


@router.get("/calendar", response_class=HTMLResponse)
def calendar(
    request: Request,
    person: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Learner agenda — upcoming offering windows and activity deadlines."""
    tenant = require_tenant(request)
    items = upcoming_for_person(db, tenant_id=tenant.id, person_id=person.id)

    # Group by the academy-local calendar day, not the stored UTC day — a
    # 00:30 WAT session is 23:30 UTC the day before and would otherwise file
    # under the wrong heading. Every rendered time already goes through localtime.
    def _local_day(item: dict):
        local = to_local(item["when"])
        return local.date() if local is not None else None

    grouped = [{"day": day, "events": list(day_items)} for day, day_items in groupby(items, key=_local_day)]
    return templates.TemplateResponse(
        request,
        "learn/calendar.html",
        {
            "request": request,
            "person": person,
            "grouped": grouped,
        },
    )
