"""Cookie-based web auth service.

Provides sign_in(), require_web_user(), and
require_web_role() for server-rendered (Jinja2) routes.
"""

# Ported at new-composition genesis (presentation-coupled wave).

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from dotmac_kernel.models import AuthSession, PartyRoleGrant, Role
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_tenant
from app.services.credential_authentication import sign_in  # noqa: F401
from app.services.identity import AcademyCurrentAccount, current_account
from app.services.security import hash_token

COOKIE = "session"


def revoke_session(db: Session, tenant_id: UUID, token: str | None) -> None:
    """Lock the genuine principal before refreshing/revoking this exact session."""
    from dotmac_kernel.identity_storage import revoke_party_sessions
    from dotmac_kernel.models import AuthSession as CanonicalSession
    from dotmac_kernel.models import Party
    from dotmac_kernel.transactions import ConflictSavepointProtocolError, conflict_savepoint

    if not token:
        return
    # begin_nested() preflushes before establishing the public SAVEPOINT. Logout
    # must never flush unrelated caller work merely to admit the storage owner.
    if db.new or db.dirty or db.deleted:
        raise ConflictSavepointProtocolError("logout requires clean pending state")
    digest = hash_token(token)
    with conflict_savepoint(db):
        candidate = db.scalars(
            select(CanonicalSession)
            .where(CanonicalSession.tenant_id == tenant_id, CanonicalSession.token_hash == digest)
            .execution_options(populate_existing=True)
        ).first()
        if candidate is None:
            return
        session_id, party_id = candidate.id, candidate.party_id
        party = db.scalars(
            select(Party)
            .where(Party.tenant_id == tenant_id, Party.id == party_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).first()
        if party is None:
            return
        session = db.scalars(
            select(CanonicalSession)
            .where(
                CanonicalSession.tenant_id == tenant_id,
                CanonicalSession.id == session_id,
                CanonicalSession.party_id == party_id,
                CanonicalSession.token_hash == digest,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).first()
        if session is not None and session.revoked_at is None:
            changed = revoke_party_sessions(
                db,
                tenant_id=tenant_id,
                party_id=party_id,
                session_ids=(session_id,),
                revoked_at=datetime.now(UTC),
            )
            if session_id in changed:
                # The owner uses Core DML. Refresh only its persisted fields on
                # the already locked row so immediate identity-map reads agree.
                db.refresh(session, attribute_names=["revoked_at", "updated_at"])


def _current_person(db: Session, tenant_id: UUID, token: str | None) -> AcademyCurrentAccount | None:
    """Resolve a cookie HMAC/session and read its current canonical account.

    Cookie authentication deliberately does not decode JWT claims. This is a
    request read, never proof that a later writer action remains authorized.
    """
    if not token:
        return None
    session = db.scalars(
        select(AuthSession)
        .where(AuthSession.tenant_id == tenant_id)
        .where(AuthSession.token_hash == hash_token(token))
        .where(AuthSession.revoked_at.is_(None))
        .where(AuthSession.expires_at > datetime.now(UTC))
        .execution_options(populate_existing=True)
    ).first()
    if session is None:
        return None
    return current_account(db, tenant_id=tenant_id, party_id=session.party_id)


def require_web_user(
    request: Request,
    db: Session = Depends(get_db),
) -> AcademyCurrentAccount:
    """Dependency: read the session cookie and return a live immutable account.

    Raises HTTPException(303) → /login when missing or invalid.
    FastAPI/Starlette's default HTTPException handler preserves the Location
    header and returns a real 303 response, so TestClient(follow_redirects=False)
    sees the redirect directly.
    """
    tenant = require_tenant(request)
    person = _current_person(db, tenant.id, request.cookies.get(COOKIE))
    if person is None:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login"},
        )
    return person


def optional_web_user(
    request: Request,
    db: Session = Depends(get_db),
) -> AcademyCurrentAccount | None:
    """Dependency: the session's current account, or None when anonymous.

    For routes that render differently for visitors and signed-in users
    (e.g. the public landing at ``/``) instead of redirecting to /login.
    """
    tenant = require_tenant(request)
    return _current_person(db, tenant.id, request.cookies.get(COOKIE))


def require_web_role(role_slug: str):
    """Return a dependency that ensures the current person holds role_slug.

    ``admin`` is a superset role: a person holding ``admin`` satisfies ANY role
    requirement (so an admin can reach instructor-gated areas like Cohorts and
    the Lab monitor without also holding the instructor role). This matches the
    Teaching area being shown to admins by the nav (``areas_for_roles``).
    """
    accepted = {role_slug, "admin"}

    def _dep(
        request: Request,
        person: AcademyCurrentAccount = Depends(require_web_user),
        db: Session = Depends(get_db),
    ) -> AcademyCurrentAccount:
        tenant = require_tenant(request)
        has = db.scalars(
            select(PartyRoleGrant)
            .join(
                Role,
                (Role.id == PartyRoleGrant.role_id) & (Role.tenant_id == PartyRoleGrant.tenant_id),
            )
            .where(PartyRoleGrant.tenant_id == tenant.id)
            .where(PartyRoleGrant.party_id == person.id)
            .where(Role.tenant_id == tenant.id)
            .where(Role.slug.in_(accepted))
            .execution_options(populate_existing=True)
        ).first()
        if has is None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
        return person

    return _dep
