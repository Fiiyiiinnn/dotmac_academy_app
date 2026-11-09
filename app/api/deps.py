"""Shared route dependencies."""

# Ported at new-composition genesis (presentation-coupled wave).

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from dotmac_kernel.deps import get_db, get_platform_db
from dotmac_kernel.models import AuthSession, PartyRoleGrant, Role, Tenant
from fastapi import Depends, HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.identity import AcademyCurrentAccount, current_account
from app.services.security import decode_access_token, hash_token


def require_tenant(request: Request) -> Tenant:
    """For routes that operate on a tenant-scoped resource."""
    tenant = getattr(request.state, "tenant", None)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return tenant


_bearer = HTTPBearer(auto_error=False)


def require_user_auth(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
    db: Session = Depends(get_db),
) -> AcademyCurrentAccount:
    """Validate JWT/session, then read the live tenant-local canonical account."""
    tenant = require_tenant(request)
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
    token = credentials.credentials

    payload = decode_access_token(token)
    if payload is None or payload.get("tenant_id") != str(tenant.id):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")

    try:
        person_id = UUID(str(payload["sub"]))
    except (KeyError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
        ) from None

    session = db.scalars(
        select(AuthSession)
        .where(AuthSession.tenant_id == tenant.id)
        .where(AuthSession.token_hash == hash_token(token))
        .where(AuthSession.revoked_at.is_(None))
        .where(AuthSession.expires_at > datetime.now(UTC))
        .execution_options(populate_existing=True)
    ).first()
    if session is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
    if session.party_id != person_id or session.tenant_id != tenant.id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")

    person = current_account(db, tenant_id=tenant.id, party_id=person_id)
    if person is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
    return person


def require_role(role_slug: str):
    """Return a dependency that requires the current person to hold `role_slug`."""

    def _dependency(
        request: Request,
        person: AcademyCurrentAccount = Depends(require_user_auth),
        db: Session = Depends(get_db),
    ) -> AcademyCurrentAccount:
        tenant = require_tenant(request)
        has_role = db.scalars(
            select(PartyRoleGrant)
            .join(
                Role,
                (Role.id == PartyRoleGrant.role_id) & (Role.tenant_id == PartyRoleGrant.tenant_id),
            )
            .where(PartyRoleGrant.tenant_id == tenant.id)
            .where(PartyRoleGrant.party_id == person.id)
            .where(Role.tenant_id == tenant.id)
            .where(Role.slug == role_slug)
            .execution_options(populate_existing=True)
        ).first()
        if has_role is None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
        return person

    return _dependency


__all__ = [
    "Depends",
    "get_db",
    "get_platform_db",
    "require_role",
    "require_tenant",
    "require_user_auth",
]
