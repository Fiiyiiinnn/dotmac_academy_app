"""Fresh cookie-bound Admin checks for these account status/role writes.

The caller owns a materialized public conflict savepoint and retains all locks
through its write. Returned identities are observations, never reusable proof.
"""

# Ported at new-composition genesis (presentation-coupled wave).

from __future__ import annotations

from typing import NoReturn
from uuid import UUID

from dotmac_kernel.identity_storage import PartyFact, lock_grants, lock_parties, lock_person, lock_roles
from dotmac_kernel.models import AuthSession, PartyType
from dotmac_kernel.transactions import ConflictSavepointProtocolError
from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import require_tenant
from app.services.canonical_identity_store import lock_profile
from app.services.identity import AcademyCurrentAccount
from app.services.roles import role_slugs
from app.services.security import hash_token
from app.services.web_auth import COOKIE, require_web_user


def require_clean_account_write(db: Session) -> None:
    """Refuse before begin_nested can preflush unrelated caller state."""
    if db.new or db.dirty or db.deleted:
        raise ConflictSavepointProtocolError("account write requires clean pending state")


def _login_required() -> NoReturn:
    raise HTTPException(status_code=303, headers={"Location": "/login"})


def revalidate_web_admin_write(
    db: Session, request: Request, *, actor_id: UUID, forbidden_detail: str
) -> AcademyCurrentAccount:
    """Read cookie/account/time again AFTER the final potentially blocking lock.

    This is called only while the caller retains the complete Party, account,
    role, grant and exact cookie-session locks acquired below. The actor UUID
    only detects a changed cookie identity; it does not confer authority.
    """
    tenant = require_tenant(request)
    actor = require_web_user(request, db)
    if actor.id != actor_id or actor.tenant_id != tenant.id:
        _login_required()
    if "admin" not in role_slugs(db, tenant.id, actor.id):
        raise HTTPException(status_code=403, detail=forbidden_detail)
    return actor


def lock_web_admin_write(
    db: Session, request: Request, *, target_id: UUID, forbidden_detail: str
) -> tuple[UUID, PartyFact | None]:
    """Identify a real cookie, then lock ALL principals before subordinate rows."""
    require_clean_account_write(db)
    tenant = require_tenant(request)
    token = request.cookies.get(COOKIE)
    if not token:
        _login_required()
    digest = hash_token(token)
    candidate = db.scalars(
        select(AuthSession)
        .where(AuthSession.tenant_id == tenant.id, AuthSession.token_hash == digest)
        .execution_options(populate_existing=True)
    ).first()
    if candidate is None:
        _login_required()
    session_id, actor_id = candidate.id, candidate.party_id
    parties = lock_parties(db, tenant_id=tenant.id, party_ids=(actor_id, target_id))
    by_id = {party.id: party for party in parties}
    complete: set[UUID] = set()
    for party in sorted(parties, key=lambda row: row.id):
        # Every existing Party is locked before the first subordinate lookup.
        person = lock_person(db, tenant_id=tenant.id, party_id=party.id)
        profile = lock_profile(db, tenant_id=tenant.id, party_id=party.id)
        if party.party_type == PartyType.person and person is not None and profile is not None:
            complete.add(party.id)
    if actor_id not in complete:
        _login_required()
    roles = lock_roles(db, tenant_id=tenant.id)
    grants = lock_grants(db, tenant_id=tenant.id, party_ids=(actor_id,))
    session = db.scalars(
        select(AuthSession)
        .where(
            AuthSession.tenant_id == tenant.id,
            AuthSession.id == session_id,
            AuthSession.party_id == actor_id,
            AuthSession.token_hash == digest,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if session is None:
        _login_required()
    # Resolve active Party/subtype/profile and the current cookie expiry before
    # classifying missing Admin as a 403 rather than invalid-login 303.
    actor = require_web_user(request, db)
    if actor.id != actor_id or actor.tenant_id != tenant.id:
        _login_required()
    admin_ids = {role.id for role in roles if role.slug == "admin"}
    if not any(grant.role_id in admin_ids for grant in grants):
        raise HTTPException(status_code=403, detail=forbidden_detail)
    return actor_id, by_id.get(target_id) if target_id in complete else None
