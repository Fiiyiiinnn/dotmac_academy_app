"""Academy admin Settings portal — configure SMTP / email toggles / branding /
lab limits in the browser (DB-stored, overriding env defaults).

Every route composes the configured Settings access roles with live tenant
grants and cookie authentication.

Settings apply to the single Academy instance and live in the
``platform_settings`` table, which only the restricted settings-writer DB role
may change. Reads use the tenant-scoped ``get_db`` session; writes use the
separate ``get_platform_db`` session so ordinary request code cannot mutate
global configuration.

The stored SMTP password is NEVER echoed back to the form; a blank password field
on POST means "keep the existing value".
"""

# Ported at new-composition genesis (web layer wave).

from __future__ import annotations

from html import escape

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_platform_db, require_tenant
from app.config import Settings, settings
from app.services.email import send_email_detailed
from app.services.identity import AcademyCurrentAccount
from app.services.settings_access import require_settings_access
from app.services.settings_store import effective, set_many
from app.web.templating import templates

router = APIRouter(
    prefix="/admin",
    dependencies=[Depends(require_tenant), Depends(require_settings_access)],
)

_DISPLAY_FIELDS = (
    "branding_name",
    "smtp_host",
    "smtp_port",
    "smtp_user",
    "smtp_from",
    "smtp_starttls",
    "email_auto_on_pass",
    "email_digest_enabled",
    "max_concurrent_labs",
    "max_concurrent_labs_per_tenant",
    "lab_idle_minutes",
)


def _display_config(cfg) -> dict[str, object]:
    """Project only form fields; credentials never cross the template boundary."""
    return {name: getattr(cfg, name) for name in _DISPLAY_FIELDS}


def _context(request: Request, db: Session) -> dict:
    cfg = effective(db)
    return {
        "request": request,
        "branding_name": cfg.branding_name,
        "cfg": _display_config(cfg),
    }


@router.get("/settings", response_class=HTMLResponse)
def settings_form(request: Request, db: Session = Depends(get_db)):
    """Render the settings form pre-filled with current effective values.

    The SMTP password is intentionally rendered BLANK (never echoed).
    """
    return templates.TemplateResponse(request, "admin/settings.html", _context(request, db))


@router.post("/settings")
def settings_save(
    request: Request,
    smtp_host: str = Form(""),
    smtp_port: str = Form(""),
    smtp_user: str = Form(""),
    smtp_from: str = Form(""),
    branding_name: str = Form(""),
    max_concurrent_labs: str = Form(""),
    max_concurrent_labs_per_tenant: str = Form(""),
    lab_idle_minutes: str = Form(""),
    smtp_starttls: str | None = Form(None),
    email_auto_on_pass: str | None = Form(None),
    email_digest_enabled: str | None = Form(None),
    platform_db: Session = Depends(get_platform_db),
):
    """Upsert settings via a platform_api session.

    `smtp_password` is environment-only in this composition and is
    refused by `set_many`; the form has no password field to store.
    """
    smtp_from = (smtp_from or "").strip()
    if smtp_user and smtp_from in {"", settings.smtp_from, Settings.model_fields["smtp_from"].default}:
        smtp_from = smtp_user.strip()
    values: dict[str, str | None] = {
        "smtp_host": smtp_host,
        "smtp_port": smtp_port,
        "smtp_user": smtp_user,
        "smtp_from": smtp_from,
        "branding_name": branding_name,
        "max_concurrent_labs": max_concurrent_labs,
        "max_concurrent_labs_per_tenant": max_concurrent_labs_per_tenant,
        "lab_idle_minutes": lab_idle_minutes,
        # Checkboxes: present => "true", absent => "false".
        "smtp_starttls": "true" if smtp_starttls is not None else "false",
        "email_auto_on_pass": "true" if email_auto_on_pass is not None else "false",
        "email_digest_enabled": "true" if email_digest_enabled is not None else "false",
    }
    # smtp_password is environment-only (settings_store env overlay).

    try:
        set_many(platform_db, values)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    cfg = effective(platform_db)

    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            request,
            "admin/_settings_save_result.html",
            {"request": request, "cfg": _display_config(cfg), "saved": True},
        )
    return RedirectResponse("/admin/settings", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/settings/test-email", response_class=HTMLResponse)
def settings_test_email(
    request: Request,
    person: AcademyCurrentAccount = Depends(require_settings_access),
    db: Session = Depends(get_db),
):
    """Send a test email to the current admin's own address; render a flash."""
    to = person.email
    cfg = effective(db)
    if not cfg.smtp_host:
        return templates.TemplateResponse(
            request,
            "admin/_test_email_result.html",
            {"request": request, "sent": False, "to": to, "unconfigured": True},
        )
    result = send_email_detailed(
        to,
        f"{cfg.branding_name} — test email",
        f"<p>This is a test email from your {escape(cfg.branding_name)} settings page.</p>",
        text_body=f"This is a test email from your {cfg.branding_name} settings page.",
        db=db,
    )
    return templates.TemplateResponse(
        request,
        "admin/_test_email_result.html",
        {
            "request": request,
            "sent": result.sent,
            "to": to,
            "unconfigured": False,
            "error": "Delivery failed" if not result.sent else "",
        },
    )
