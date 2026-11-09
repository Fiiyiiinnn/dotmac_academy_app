"""Purpose-specific product access over fresh canonical cookie identity and grants."""

# Ported at new-composition genesis (presentation-coupled wave).
from typing import cast

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_tenant
from app.services.identity import AcademyCurrentAccount
from app.services.roles import role_slugs
from app.services.settings_store import effective
from app.services.web_auth import require_web_user


def require_admin_home_access(
    request: Request,
    account: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
) -> AcademyCurrentAccount:
    tenant = require_tenant(request)
    policy = effective(db, require_store=True)
    if not set(policy.admin_home_access_roles).intersection(role_slugs(db, tenant.id, account.id)):
        raise HTTPException(status_code=403, detail="Forbidden")
    return account


def require_lab_admin_access(
    request: Request,
    account: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
) -> AcademyCurrentAccount:
    tenant = require_tenant(request)
    policy = effective(db, require_store=True)
    if not set(policy.lab_admin_access_roles).intersection(role_slugs(db, tenant.id, account.id)):
        raise HTTPException(status_code=403, detail="Forbidden")
    return account


def _require_bank_roles(
    request: Request,
    account: AcademyCurrentAccount,
    db: Session,
    key: str,
) -> AcademyCurrentAccount:
    tenant = require_tenant(request)
    policy = effective(db, require_store=True)
    if not set(cast(tuple[str, ...], policy[key])).intersection(role_slugs(db, tenant.id, account.id)):
        raise HTTPException(status_code=403, detail="Forbidden")
    return account


def require_bank_author_access(
    request: Request,
    account: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
) -> AcademyCurrentAccount:
    """Draft create/edit/read gate; course assignment is checked per bank."""
    return _require_bank_roles(request, account, db, "bank_author_roles")


def require_bank_publish_access(
    request: Request,
    account: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
) -> AcademyCurrentAccount:
    return _require_bank_roles(request, account, db, "bank_publish_roles")


def require_bank_repoint_access(
    request: Request,
    account: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
) -> AcademyCurrentAccount:
    return _require_bank_roles(request, account, db, "bank_repoint_roles")


def require_bank_export_access(
    request: Request,
    account: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
) -> AcademyCurrentAccount:
    return _require_bank_roles(request, account, db, "bank_export_roles")


def require_bank_retire_access(
    request: Request,
    account: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
) -> AcademyCurrentAccount:
    return _require_bank_roles(request, account, db, "bank_retire_roles")


def require_reminders_admin_access(
    request: Request,
    account: AcademyCurrentAccount = Depends(require_web_user),
    db: Session = Depends(get_db),
) -> AcademyCurrentAccount:
    tenant = require_tenant(request)
    policy = effective(db, require_store=True)
    if not set(policy.reminders_admin_access_roles).intersection(role_slugs(db, tenant.id, account.id)):
        raise HTTPException(status_code=403, detail="Forbidden")
    return account
