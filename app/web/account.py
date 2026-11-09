"""Account area — the signed-in user's own profile and password (Task 7).

Every route requires a valid session (``require_web_user``); any logged-in user
(student / instructor / admin) may manage their own account. All queries are
tenant-scoped via ``require_tenant`` + the RLS-bound ``get_db`` session.

IMPORTANT: no ``db.commit()`` inside any handler — ``get_db`` owns the
transaction and commits after the response (a mid-handler commit would clear the
RLS ``app.current_tenant`` GUC).
"""

# Ported at new-composition genesis (web layer wave).

from __future__ import annotations

from pathlib import Path

from dotmac_learning.participation_models import Cohort, Enrollment
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_tenant
from app.models.reminder import EVENT_KINDS
from app.services import account_profile
from app.services import reminders as reminders_service
from app.services.identity import AcademyCurrentAccount
from app.services.password_change import change_password
from app.services.profile_policy import profile_policy
from app.services.roles import role_slugs
from app.services.web_auth import require_web_user
from app.web.templating import templates

router = APIRouter(dependencies=[Depends(require_tenant)])

# Avatar uploads land under the static mount so they serve straight off disk.
AVATAR_ROOT = Path("static/avatars")


def _flash(request: Request, ok: bool, message: str) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "account/_flash.html",
        {"request": request, "ok": ok, "message": message},
    )


def _cohort_names(db: Session, tenant_id, person_id) -> list[str]:
    # Explicit remaining product UI read gate: public Learning APIs inspected
    # have no matching all-enrollment, alphabetized, names-only operation.
    return list(
        db.scalars(
            select(Cohort.name)
            .join(
                Enrollment,
                (Enrollment.cohort_id == Cohort.id) & (Enrollment.tenant_id == Cohort.tenant_id),
            )
            .where(Cohort.tenant_id == tenant_id)
            .where(Enrollment.person_id == person_id)
            .order_by(Cohort.name)
        ).all()
    )


def _profile_context(request: Request, db: Session, person: AcademyCurrentAccount) -> dict:
    tenant = require_tenant(request)
    return {
        "request": request,
        "person": person,
        "roles": sorted(role_slugs(db, tenant.id, person.id)),
        "cohorts": _cohort_names(db, tenant.id, person.id),
        "profile_policy": profile_policy(db),
    }


@router.get("/account", response_class=HTMLResponse)
def profile_form(
    request: Request,
    person: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(request, "account/profile.html", _profile_context(request, db, person))


@router.post("/account", response_class=HTMLResponse)
def profile_save(
    request: Request,
    first_name: str = Form(...),
    last_name: str = Form(...),
    person: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
):
    """Update self names and their canonical display projection; email stays read-only."""
    try:
        account_profile.save_names(db, request=request, first_name=first_name, last_name=last_name)
    except account_profile.ProfileInputError as exc:
        return _flash(request, False, str(exc))
    return _flash(request, True, "Profile updated.")


@router.get("/account/password", response_class=HTMLResponse)
def password_form(
    request: Request,
    person: AcademyCurrentAccount = Depends(require_web_user),
):
    return templates.TemplateResponse(request, "account/password.html", {"request": request, "person": person})


@router.post("/account/password", response_class=HTMLResponse)
def password_change(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    person: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
):
    result = change_password(
        db,
        request=request,
        current_password=current_password,
        new_password=new_password,
        confirm_password=confirm_password,
    )
    if result.code == "current_password_invalid":
        return _flash(request, False, "Current password is incorrect.")
    if result.code == "confirmation_mismatch":
        return _flash(request, False, "New passwords do not match.")
    if result.code == "policy_rejected":
        return _flash(request, False, f"New password must be at least {result.min_length} characters.")
    return _flash(request, True, "Password updated.")


@router.post("/account/avatar", response_class=HTMLResponse)
def avatar_upload(
    request: Request,
    file: UploadFile = File(...),
    person: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
):
    """Replace the proved self avatar and refresh the topbar/profile preview."""
    try:
        account_profile.save_avatar(
            db, request=request, stream=file.file, content_type=file.content_type, avatar_root=AVATAR_ROOT
        )
    except account_profile.ProfileInputError as exc:
        return _flash(request, False, str(exc))
    resp = _flash(request, True, "Photo updated.")
    resp.headers["HX-Refresh"] = "true"
    return resp


@router.get("/account/notifications", response_class=HTMLResponse)
def notifications_form(
    request: Request,
    person: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
):
    tenant = require_tenant(request)
    reminder_pref = reminders_service.get_preference(db, tenant_id=tenant.id, person_id=person.id)
    return templates.TemplateResponse(
        request,
        "account/notifications.html",
        {
            "request": request,
            "person": person,
            "prefs": person.prefs or {},
            "profile_policy": profile_policy(db),
            "reminder_pref": reminder_pref,
            "reminder_kinds": EVENT_KINDS,
        },
    )


@router.post("/account/reminders", response_class=HTMLResponse)
async def reminders_save(
    request: Request,
    person: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
):
    """Persist reminder frequency, quiet hours, and per-event opt-outs.

    Opt-out checkboxes arrive as ``optout_<kind>`` fields; checked means the
    person does NOT want that event kind. Async so the dynamic checkbox names
    can be read from the form body directly.
    """
    tenant = require_tenant(request)
    form = await request.form()
    optouts = [k for k in EVENT_KINDS if form.get(f"optout_{k}")]

    def _hour(raw: object) -> int | None:
        text = str(raw or "").strip()
        return int(text) if text else None

    try:
        reminders_service.save_preference(
            db,
            tenant_id=tenant.id,
            person_id=person.id,
            frequency=str(form.get("frequency", "immediate")),
            quiet_start_hour=_hour(form.get("quiet_start_hour")),
            quiet_end_hour=_hour(form.get("quiet_end_hour")),
            optouts=optouts,
        )
    except ValueError as exc:
        return _flash(request, False, str(exc))
    return _flash(request, True, "Reminder preferences saved.")


@router.post("/account/notifications", response_class=HTMLResponse)
def notifications_save(
    request: Request,
    email_results: str | None = Form(None),
    email_digest: str | None = Form(None),
    person: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
):
    """Merge the two explicit form opt-ins into the proved self product profile."""
    try:
        account_profile.save_preferences(
            db, request=request, email_results=email_results is not None, email_digest=email_digest is not None
        )
    except account_profile.ProfileInputError as exc:
        return _flash(request, False, str(exc))
    return _flash(request, True, "Notification preferences saved.")
