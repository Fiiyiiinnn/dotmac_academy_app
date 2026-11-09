"""Canonical request/reset and product account-status transitions.

HTTP status writes revalidate cookie-bound Admin authority under common locks.
The actorless status entry is trusted internal application code only. Hosted
whole-writer/assembly qualification remains separate; this is no activation claim.
The caller owns the root transaction and token delivery. No external I/O occurs.
"""

# Ported at new-composition genesis (presentation-coupled wave).

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

from dotmac_kernel.credential_lifecycle import CredentialLifecycleError
from dotmac_kernel.identity_storage import revoke_party_sessions
from dotmac_kernel.models import AuthSession, Party, PartyPerson, PartyType, UserCredential
from dotmac_kernel.transactions import conflict_savepoint, conflict_savepoint_requires_rollback
from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, SessionTransaction

from app.api.deps import require_tenant
from app.models.account_token import AccountToken
from app.models.identity_extensions import AcademyAccountProfile
from app.services.credential_lifecycle import complete_password_reset
from app.services.credential_policy import password_reset_policy, password_reset_token_policy
from app.services.exceptions import BadRequestError
from app.services.identity import canonical_party_for_email, normalize_email
from app.services.security import hash_token
from app.services.web_account_authority import (
    lock_web_admin_write,
    require_clean_account_write,
    revalidate_web_admin_write,
)


def _lock_account(db: Session, *, tenant_id: UUID, party_id: UUID) -> tuple[Party, AcademyAccountProfile] | None:
    """Refresh each genuine account component in the shared principal-first order."""
    party = db.scalars(
        select(Party)
        .where(Party.tenant_id == tenant_id, Party.id == party_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if party is None or party.party_type != PartyType.person:
        return None
    person = db.scalars(
        select(PartyPerson)
        .where(PartyPerson.party_id == party.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    profile = db.scalars(
        select(AcademyAccountProfile)
        .where(AcademyAccountProfile.tenant_id == tenant_id, AcademyAccountProfile.party_id == party.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if person is None or profile is None:
        return None
    return party, profile


def _require_recovered_root(db: Session, root: SessionTransaction | None) -> None:
    # Translate only after the public savepoint restored the same caller root.
    if (
        not db.is_active
        or root is None
        or db.get_transaction() is not root
        or conflict_savepoint_requires_rollback(db, expected_root=cast(SessionTransaction, root))
    ):
        raise CredentialLifecycleError("canonical lifecycle requires caller transaction rollback") from None


def request_password_reset(db: Session, *, tenant_id: UUID, email: str, now: datetime | None = None) -> str | None:
    """Issue a hashed proof for a genuine credential-bearing account.

    Callers must give identical responses for unknown and known email addresses.
    Earlier outstanding reset proofs stay valid until a reset consumes siblings.
    """
    root = db.get_transaction()
    try:
        with conflict_savepoint(db):
            root = db.get_transaction()
            canonical = normalize_email(email)
            candidate = canonical_party_for_email(db, tenant_id=tenant_id, email=canonical)
            if candidate is None:
                return None
            account = _lock_account(db, tenant_id=tenant_id, party_id=candidate.id)
            if account is None:
                return None
            party, _ = account
            # Identification was unlocked. Recheck ownership after waiting,
            # rather than granting the original email access to a changed Party.
            if party.tenant_id != tenant_id or normalize_email(party.email) != canonical:
                return None
            credential = db.scalars(
                select(UserCredential)
                .where(UserCredential.tenant_id == tenant_id, UserCredential.party_id == party.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).first()
            if credential is None:
                return None
            policy = password_reset_token_policy(db)
            issued_at = now if now is not None else datetime.now(UTC)
            expires_at = policy.deadline(issued_at)
            raw = uuid4().hex + uuid4().hex
            db.add(
                AccountToken(
                    tenant_id=tenant_id,
                    person_id=party.id,
                    kind="password_reset",
                    token_hash=hash_token(raw),
                    expires_at=expires_at,
                )
            )
            db.flush()
        return raw
    except IntegrityError:
        _require_recovered_root(db, root)
        raise BadRequestError("account lifecycle conflict") from None


def reset_password(db: Session, *, tenant_id: UUID, raw: str, new_password: str, now: datetime | None = None) -> Party:
    """Compose actual product policy into the existing public account-reset owner."""
    return complete_password_reset(
        db,
        tenant_id=tenant_id,
        raw=raw,
        new_password=new_password,
        password_policy=password_reset_policy(db),
        now=now,
    )


def set_account_status(
    db: Session, *, tenant_id: UUID, person_id: UUID, status: str, now: datetime | None = None
) -> AcademyAccountProfile:
    """Trusted INTERNAL transition; HTTP callers must use set_web_account_status.

    Product suspension changes profile status, never canonical Party.is_active.
    Reactivation creates no sessions and preserves prior revocation timestamps.
    """
    return _set_account_status(db, tenant_id=tenant_id, person_id=person_id, status=status, now=now)


def set_web_account_status(
    db: Session, request: Request, *, person_id: UUID, status: str, forbidden_detail: str
) -> AcademyAccountProfile:
    """Request-bound status owner; a dependency's read projection is insufficient."""
    return _set_account_status(
        db,
        tenant_id=require_tenant(request).id,
        person_id=person_id,
        status=status,
        request=request,
        forbidden_detail=forbidden_detail,
    )


def _set_account_status(
    db: Session,
    *,
    tenant_id: UUID,
    person_id: UUID,
    status: str,
    now: datetime | None = None,
    request: Request | None = None,
    forbidden_detail: str = "Forbidden",
) -> AcademyAccountProfile:
    if status not in {"active", "suspended"}:
        raise BadRequestError(f"invalid account status: {status}")
    require_clean_account_write(db)
    root = db.get_transaction()
    try:
        with conflict_savepoint(db):
            root = db.get_transaction()
            db.connection()  # Materialize SAVEPOINT before the immediate-Core owner.
            actor_id = None
            if request is None:
                account = _lock_account(db, tenant_id=tenant_id, party_id=person_id)
                if account is None:
                    raise BadRequestError("person not found")
                party, profile = account
                party_id = party.id
            else:
                actor_id, target = lock_web_admin_write(
                    db,
                    request,
                    target_id=person_id,
                    forbidden_detail=forbidden_detail,
                )
                if target is None:
                    raise BadRequestError("person not found")
                party_id = target.id
                profile_row = db.scalars(
                    select(AcademyAccountProfile)
                    .where(AcademyAccountProfile.tenant_id == tenant_id, AcademyAccountProfile.party_id == party_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                ).first()
                if profile_row is None:
                    raise BadRequestError("person not found")
                profile = profile_row
            sessions: Sequence[AuthSession] = []
            if status == "suspended":
                sessions = db.scalars(
                    select(AuthSession)
                    .where(
                        AuthSession.tenant_id == tenant_id,
                        AuthSession.party_id == party_id,
                        AuthSession.revoked_at.is_(None),
                    )
                    .order_by(AuthSession.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                ).all()
            if request is not None and actor_id is not None:
                revalidate_web_admin_write(db, request, actor_id=actor_id, forbidden_detail=forbidden_detail)
            changed_at = now if now is not None else datetime.now(UTC)
            if status == "suspended":
                revoke_party_sessions(
                    db,
                    tenant_id=tenant_id,
                    party_id=party_id,
                    session_ids=tuple(session.id for session in sessions),
                    revoked_at=changed_at,
                )
                for session in sessions:
                    db.refresh(session, attribute_names=["revoked_at", "updated_at"])
            profile.status = status
            profile.updated_at = changed_at
            db.flush()
        return profile
    except IntegrityError:
        _require_recovered_root(db, root)
        raise BadRequestError("account lifecycle conflict") from None
    except (BadRequestError, HTTPException):
        _require_recovered_root(db, root)
        raise
