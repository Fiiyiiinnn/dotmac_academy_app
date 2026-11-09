# Ported at new-composition genesis (web layer wave).
# app/web/accounts.py
"""Account-creation portal routes — create login-capable accounts.

User management lives in the Admin area (/admin/users) but this is a SEPARATE
router from app/web/settings.py: it is gated inline (instructor OR admin), NOT
by require_web_role("admin"), so an instructor can still view the list and
provision student accounts. Creating an instructor/admin account remains
admin-only (privilege-escalation guard below).

Gating rules:
  * GET/POST /admin/users — reachable by instructor OR admin (else 403).
  * POST creating a "student" — any of the above.
  * POST creating an "instructor" or "admin" — requires the actor to be admin.

IMPORTANT: no db.commit() inside any handler — get_db owns the transaction and
commits after the response (a mid-handler commit clears the RLS tenant GUC).
"""

from __future__ import annotations

from html import escape
from types import SimpleNamespace
from typing import TYPE_CHECKING
from uuid import UUID

from dotmac_kernel.exceptions import BadRequestError, ForbiddenError
from dotmac_kernel.identity_storage import ensure_grant, lock_grants, remove_grants
from dotmac_kernel.models import Party, PartyPerson, PartyType
from dotmac_kernel.transactions import conflict_savepoint
from dotmac_learning.models import Course
from dotmac_learning.participation_models import Cohort, CourseOffering
from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_tenant
from app.models.identity_extensions import AcademyAccountProfile
from app.services import tracks as track_svc
from app.services.account_invitations import CohortAssignment
from app.services.bootstrap import ensure_roles
from app.services.canonical_lifecycle import request_password_reset, set_web_account_status
from app.services.credential_assignment import create_web_user
from app.services.email_outbox import enqueue_email
from app.services.identity import AcademyCurrentAccount
from app.services.invitation_issuance import (
    cohort_fact,
    course_fact,
    invite_web_account,
    resend_web_account_invitation,
    track_fact,
)
from app.services.roles import role_slugs
from app.services.security import hash_token
from app.services.web_account_authority import (
    lock_web_admin_write,
    require_clean_account_write,
    revalidate_web_admin_write,
)
from app.services.web_auth import require_web_user
from app.web.email_links import absolute_surface_link
from app.web.responses import hx_redirect
from app.web.templating import templates

if TYPE_CHECKING:
    from dotmac_learning.references import CourseFact

router = APIRouter(prefix="/admin/users", dependencies=[Depends(require_tenant)])


def _role_slugs(db: Session, tenant_id: UUID, person_id: UUID) -> set[str]:
    """Return the set of role slugs held by the person within the tenant.

    Thin wrapper kept for backwards compatibility; delegates to the shared
    `app.services.roles.role_slugs`.
    """
    return role_slugs(db, tenant_id, person_id)


def _html_error(message: str, status_code: int = status.HTTP_200_OK) -> HTMLResponse:
    return HTMLResponse(
        f'<div class="rounded-lg bg-clay-500/15 p-3 text-sm font-semibold text-clay-600">{escape(message)}</div>',
        status_code=status_code,
    )


@router.get("", response_class=HTMLResponse)
def users_list(
    request: Request,
    q: str = "",
    person: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
):
    tenant = require_tenant(request)
    slugs = _role_slugs(db, tenant.id, person.id)
    if not ({"instructor", "admin"} & slugs):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    is_admin = "admin" in slugs

    normalized_q = (q or "").strip()
    people_query = (
        select(
            Party.id,
            Party.email,
            PartyPerson.first_name,
            PartyPerson.last_name,
            AcademyAccountProfile.status.label("status"),
        )
        .select_from(Party)
        .join(PartyPerson, PartyPerson.party_id == Party.id)
        .outerjoin(
            AcademyAccountProfile,
            (AcademyAccountProfile.party_id == Party.id) & (AcademyAccountProfile.tenant_id == Party.tenant_id),
        )
        .where(Party.tenant_id == tenant.id)
        .where(Party.party_type == PartyType.person)
    )
    if normalized_q:
        like = f"%{normalized_q}%"
        people_query = people_query.where(
            or_(
                Party.email.ilike(like),
                PartyPerson.first_name.ilike(like),
                PartyPerson.last_name.ilike(like),
                (PartyPerson.first_name + " " + PartyPerson.last_name).ilike(like),
            )
        )
    people = [
        SimpleNamespace(
            id=row.id,
            email=row.email,
            first_name=row.first_name,
            last_name=row.last_name,
            status=row.status or "",
        )
        for row in db.execute(people_query.order_by(Party.email))
    ]
    cohorts = db.scalars(
        select(Cohort).where(Cohort.tenant_id == tenant.id).where(Cohort.status == "active").order_by(Cohort.name)
    ).all()
    courses = db.scalars(select(Course).where(Course.tenant_id == tenant.id).order_by(Course.title)).all()
    track_groups = track_svc.tracks_for_cohorts(db, tenant_id=tenant.id, cohort_ids=[cohort.id for cohort in cohorts])
    cohort_track_groups = [{"cohort": cohort, "tracks": track_groups.get(cohort.id, [])} for cohort in cohorts]
    offering_rows = db.execute(
        select(CourseOffering, Course)
        .join(
            Course,
            (Course.id == CourseOffering.course_id) & (Course.tenant_id == CourseOffering.tenant_id),
        )
        .where(CourseOffering.tenant_id == tenant.id)
        .where(CourseOffering.status == "active")
        .order_by(Course.title)
    ).all()
    courses_by_cohort: dict[UUID, list[Course]] = {cohort.id: [] for cohort in cohorts}
    seen_offerings: set[tuple[UUID, UUID]] = set()
    for offering, course in offering_rows:
        key = (offering.cohort_id, course.id)
        if offering.cohort_id in courses_by_cohort and key not in seen_offerings:
            courses_by_cohort[offering.cohort_id].append(course)
            seen_offerings.add(key)
    cohort_course_groups = [{"cohort": cohort, "courses": courses_by_cohort.get(cohort.id, [])} for cohort in cohorts]
    rows = [
        {
            "person": row,
            "roles": sorted(_role_slugs(db, tenant.id, row.id)),
        }
        for row in people
    ]
    return templates.TemplateResponse(
        request,
        "admin/users.html",
        {
            "request": request,
            "users": rows,
            "is_admin": is_admin,
            "cohorts": cohorts,
            "courses": courses,
            "cohort_course_groups": cohort_course_groups,
            "cohort_track_groups": cohort_track_groups,
            "search_query": normalized_q,
        },
    )


@router.post("/invite", response_class=HTMLResponse)
def users_invite(
    request: Request,
    first_name: str = Form(...),
    last_name: str = Form(...),
    email: str = Form(...),
    role: str = Form(...),
    cohort_ids: list[str] = Form([]),
    cohort_id: str = Form(""),
    track_ids: list[str] = Form([]),
    track_id: str = Form(""),
    course_ids: list[str] = Form([]),
    course_id: str = Form(""),
    person: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
):
    tenant = require_tenant(request)
    slugs = _role_slugs(db, tenant.id, person.id)
    if "admin" not in slugs:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only admins can invite users")
    if role not in {"student", "instructor", "admin"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid role")

    cohorts = []
    courses_by_cohort: dict[UUID, list[CourseFact]] = {}
    tracks_by_cohort = {}
    selected_cohort_ids = [raw for raw in [*cohort_ids, cohort_id] if raw]
    selected_track_ids = [raw for raw in [*track_ids, track_id] if raw]
    selected_course_ids = [raw for raw in [*course_ids, course_id] if raw]
    seen_cohort_ids: set[UUID] = set()
    for raw_cohort_id in selected_cohort_ids:
        try:
            cohort_uuid = UUID(raw_cohort_id)
        except ValueError:
            return _html_error("Invalid cohort selection")
        if cohort_uuid in seen_cohort_ids:
            continue
        cohort = cohort_fact(db, tenant_id=tenant.id, cohort_id=cohort_uuid, active_only=True)
        if cohort is None:
            return _html_error("Selected cohort was not found")
        seen_cohort_ids.add(cohort_uuid)
        cohorts.append(cohort)
        courses_by_cohort[cohort_uuid] = []
    if selected_track_ids:
        if not cohorts:
            return _html_error("Select at least one cohort before assigning tracks")
        for raw_track_id in selected_track_ids:
            raw_track_id = raw_track_id.strip()
            if ":" not in raw_track_id:
                return _html_error("Invalid track selection")
            raw_cohort_id, raw_track_uuid = raw_track_id.split(":", 1)
            try:
                pair_cohort_id = UUID(raw_cohort_id)
                track_uuid = UUID(raw_track_uuid)
            except ValueError:
                return _html_error("Invalid track selection")
            if pair_cohort_id not in seen_cohort_ids:
                return _html_error("Selected track does not belong to a selected cohort")
            if pair_cohort_id in tracks_by_cohort:
                return _html_error("Select only one track per cohort")
            cohort_track = track_svc.cohort_track_or_404(
                db, tenant_id=tenant.id, cohort_id=pair_cohort_id, track_id=track_uuid
            )
            track = track_fact(db, tenant_id=tenant.id, cohort_id=pair_cohort_id, track_id=cohort_track.track_id)
            if track is None:
                return _html_error("Selected track was not found")
            tracks_by_cohort[pair_cohort_id] = track

    if selected_course_ids:
        if tracks_by_cohort:
            return _html_error("Direct course assignment cannot be combined with track assignment")
        if not cohorts:
            return _html_error("Select at least one cohort before assigning courses")
        seen_course_pairs: set[tuple[UUID, UUID]] = set()
        for raw_course_id in selected_course_ids:
            raw_course_id = raw_course_id.strip()
            target_cohort_ids = list(seen_cohort_ids)
            if ":" in raw_course_id:
                raw_cohort_id, raw_course_uuid = raw_course_id.split(":", 1)
                try:
                    pair_cohort_id = UUID(raw_cohort_id)
                except ValueError:
                    return _html_error("Invalid cohort selection")
                if pair_cohort_id not in seen_cohort_ids:
                    return _html_error("Selected course does not belong to a selected cohort")
                target_cohort_ids = [pair_cohort_id]
            else:
                raw_course_uuid = raw_course_id
            try:
                course_uuid = UUID(raw_course_uuid)
            except ValueError:
                return _html_error("Invalid course selection")
            course = course_fact(db, tenant_id=tenant.id, course_id=course_uuid)
            if course is None:
                return _html_error("Selected course was not found")
            for target_cohort_id in target_cohort_ids:
                pair = (target_cohort_id, course_uuid)
                if pair in seen_course_pairs:
                    continue
                seen_course_pairs.add(pair)
                courses_by_cohort.setdefault(target_cohort_id, []).append(course)

    with invite_web_account(
        db,
        request=request,
        email=email,
        first_name=first_name,
        last_name=last_name,
        role=role,
        assignments=tuple(
            CohortAssignment(
                cohort_id=cohort.id,
                course_ids=tuple(course.id for course in courses_by_cohort.get(cohort.id, [])),
                track_id=tracks_by_cohort[cohort.id].id if cohort.id in tracks_by_cohort else None,
            )
            for cohort in cohorts
        ),
    ) as result:
        invited = result.person
        token = result.token
        existing_user = not result.created_person
        assignments = result.assignments
        link = absolute_surface_link(request, "accept_form", token=token) if token else ""
        assignment_html = ""
        assignment_text = ""
        if assignments:
            assignment_items = "".join(f"<li>{escape(item)}</li>" for item in assignments)
            assignment_html = f"<p>Assignments:</p><ul>{assignment_items}</ul>"
            assignment_text = "\nAssignments: " + "; ".join(assignments) + "\n"
        sent = False
        if token:
            sent = enqueue_email(
                db,
                tenant_id=tenant.id,
                idempotency_key=f"account-invite:{invited.id}:{hash_token(token)}",
                kind="account_invite",
                recipient=invited.email,
                subject="You're invited to Dotmac Academy",
                html_body=(
                    f"<p>Hi {escape(invited.first_name)},</p>"
                    f"<p>You have been invited to Dotmac Academy as <strong>{escape(role)}</strong>.</p>"
                    f"{assignment_html}"
                    f'<p><a href="{link}">Set up your account</a></p>'
                    f"<p>If the button does not work, open this link: {link}</p>"
                ),
                text_body=(
                    f"Hi {invited.first_name},\n\n"
                    f"You have been invited to Dotmac Academy as {role}.\n"
                    f"{assignment_text}\n"
                    f"Set up your account: {link}\n"
                ),
            )
        if existing_user and token:
            if sent:
                status_text = "User already existed. Invite queued and assignments updated."
            else:
                status_text = "User already existed. Assignments updated; invite could not be queued."
        elif existing_user:
            status_text = "User already exists. Assignments updated."
        else:
            status_text = "Invite queued." if sent else "Invite could not be queued."
        assignment_status = ""
        if assignments:
            items = "".join(f"<li>{escape(item)}</li>" for item in assignments)
            assignment_status = f'<ul class="mt-1 list-disc pl-5 text-ink-soft">{items}</ul>'
        return HTMLResponse(
            f'<div class="rounded-lg bg-sand-100 p-3 text-sm" role="status">'
            f'<p class="font-semibold">{status_text}</p>'
            f"<p>{escape(invited.email)} - {escape(role)}</p>"
            f"{assignment_status}"
            f"</div>"
        )


@router.post("/{person_id}/reset-link", response_class=HTMLResponse)
def users_reset_link(
    person_id: UUID,
    request: Request,
    actor: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
):
    tenant = require_tenant(request)
    slugs = _role_slugs(db, tenant.id, actor.id)
    if "admin" not in slugs:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only admins can send reset links")
    target_row = db.execute(
        select(Party.id, Party.email, PartyPerson.first_name)
        .select_from(Party)
        .join(PartyPerson, PartyPerson.party_id == Party.id)
        .where(Party.tenant_id == tenant.id, Party.id == person_id, Party.party_type == PartyType.person)
    ).first()
    target = (
        SimpleNamespace(id=target_row.id, email=target_row.email, first_name=target_row.first_name)
        if target_row is not None
        else None
    )
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    token = request_password_reset(db, tenant_id=tenant.id, email=target.email)
    if token is None:
        return HTMLResponse(
            (
                '<div class="rounded-lg bg-clay-500/15 p-3 text-sm font-semibold text-clay-600">'
                "No account credential exists yet. Use an invite link instead.</div>"
            ),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    link = absolute_surface_link(request, "reset_form", token=token)
    sent = enqueue_email(
        db,
        tenant_id=tenant.id,
        idempotency_key=f"password-reset:{target.id}:{hash_token(token)}",
        kind="password_reset",
        recipient=target.email,
        subject="Reset your Dotmac Academy password",
        html_body=(
            f"<p>Hi {target.first_name},</p>"
            f"<p>Use this link to reset your password:</p>"
            f'<p><a href="{link}">Reset password</a></p>'
            f"<p>If the button does not work, open this link: {link}</p>"
        ),
        text_body=f"Reset your Dotmac Academy password: {link}\n",
    )
    status_text = "Reset email queued." if sent else "Reset email could not be queued."
    return HTMLResponse(
        f'<div class="rounded-lg bg-sand-100 p-3 text-sm" role="status">'
        f'<p class="font-semibold">{status_text}</p>'
        f"</div>"
    )


@router.post("/{person_id}/invite-link", response_class=HTMLResponse)
def users_invite_link(
    person_id: UUID,
    request: Request,
    actor: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
):
    tenant = require_tenant(request)
    slugs = _role_slugs(db, tenant.id, actor.id)
    if "admin" not in slugs:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only admins can send invites")
    with resend_web_account_invitation(db, request=request, person_id=person_id) as result:
        target = result.person
        if result.had_credential:
            return HTMLResponse(
                (
                    '<div class="rounded-lg bg-sand-100 p-3 text-sm">'
                    '<p class="font-semibold">This user already has a password.</p>'
                    '<p class="text-ink-soft">Use a reset link if they need access restored.</p>'
                    "</div>"
                )
            )
        token = result.token
        link = absolute_surface_link(request, "accept_form", token=token)
        sent = enqueue_email(
            db,
            tenant_id=tenant.id,
            idempotency_key=f"account-invite:{target.id}:{hash_token(token)}",
            kind="account_invite",
            recipient=target.email,
            subject="Activate your Dotmac Academy account",
            html_body=(
                f"<p>Hi {escape(target.first_name)},</p>"
                "<p>Use this link to activate your Dotmac Academy account:</p>"
                f'<p><a href="{link}">Set up your account</a></p>'
                f"<p>If the button does not work, open this link: {link}</p>"
            ),
            text_body=f"Set up your Dotmac Academy account: {link}\n",
        )
        status_text = "Invite email queued." if sent else "Invite email could not be queued."
        return HTMLResponse(
            f'<div class="rounded-lg bg-sand-100 p-3 text-sm" role="status">'
            f'<p class="font-semibold">{status_text}</p>'
            f"</div>"
        )


@router.post("/{person_id}/role")
def users_role(
    person_id: UUID,
    request: Request,
    role: str = Form(...),
    actor: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
):
    tenant = require_tenant(request)
    require_clean_account_write(db)
    slugs = _role_slugs(db, tenant.id, actor.id)
    if "admin" not in slugs:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only admins can change roles")
    if role not in {"student", "instructor", "admin"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid role")
    with conflict_savepoint(db):
        db.connection()
        actor_id, target = lock_web_admin_write(
            db,
            request,
            target_id=person_id,
            forbidden_detail="Only admins can change roles",
        )
        if target is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        if target.id == actor_id and role != "admin":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="You cannot remove your own admin role here",
            )
        roles = ensure_roles(db, tenant.id)
        standard_role_ids = {roles[slug].id for slug in ("student", "instructor", "admin")}
        target_role_id = roles[role].id
        existing_grants = lock_grants(db, tenant_id=tenant.id, party_ids=(target.id,))
        obsolete = tuple(
            grant for grant in existing_grants if grant.role_id in standard_role_ids and grant.role_id != target_role_id
        )
        has_target_role = any(grant.role_id == target_role_id for grant in existing_grants)
        # Role provisioning and the final grant lock can wait past cookie expiry.
        revalidate_web_admin_write(db, request, actor_id=actor_id, forbidden_detail="Only admins can change roles")
        remove_grants(db, tenant_id=tenant.id, party_id=target.id, expected=obsolete)
        if not has_target_role:
            ensure_grant(
                db,
                tenant_id=tenant.id,
                party_id=target.id,
                role_id=target_role_id,
                new_grant_id=None,
                created_at=None,
                updated_at=None,
            )
        db.flush()
    return hx_redirect(request, "/admin/users")


@router.post("/{person_id}/status", response_class=HTMLResponse)
def users_status(
    person_id: UUID,
    request: Request,
    status_value: str = Form(...),
    actor: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
):
    tenant = require_tenant(request)
    require_clean_account_write(db)
    slugs = _role_slugs(db, tenant.id, actor.id)
    if "admin" not in slugs:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only admins can manage accounts")
    set_web_account_status(
        db,
        request,
        person_id=person_id,
        status=status_value,
        forbidden_detail="Only admins can manage accounts",
    )
    label = "suspended" if status_value == "suspended" else "reactivated"
    return HTMLResponse(
        (
            '<span class="inline-flex items-center rounded-full bg-brand-100 px-2.5 py-0.5 '
            f'text-xs font-semibold text-brand-800">Account {label}</span>'
        )
    )


@router.post("")
def users_create(
    request: Request,
    first_name: str = Form(...),
    last_name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    role: str = Form(...),
    person: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
):
    tenant = require_tenant(request)
    slugs = _role_slugs(db, tenant.id, person.id)
    if not ({"instructor", "admin"} & slugs):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    # Privilege escalation guard: only admins may mint instructor/admin accounts.
    if role in {"instructor", "admin"} and "admin" not in slugs:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can create instructor or admin accounts",
        )

    try:
        create_web_user(
            db,
            request=request,
            email=email,
            first_name=first_name,
            last_name=last_name,
            password=password,
            role=role,
        )
    except ForbiddenError as exc:
        # Canonical creation refuses by category: authorization refusals stay
        # 403, input/conflict refusals stay 400 (the legacy ValueError split).
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden") from exc
    except BadRequestError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    # No db.commit() here — get_db commits after the response is returned.
    return hx_redirect(request, "/admin/users")
