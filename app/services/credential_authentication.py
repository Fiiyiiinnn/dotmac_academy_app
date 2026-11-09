"""Canonical verification and session insertion in the caller's root transaction.

Source only. Every identity/reset/status/revocation writer must use the same
principal-first locking protocol before this slice can be activated. Account
projections are response data, never enduring authorization.
"""

# Ported at new-composition genesis (accounts-core slice).

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from dotmac_kernel.credential_lifecycle import (
    CredentialCohortDrift,
    CredentialLifecycle,
    CredentialLifecycleError,
    CredentialSnapshot,
    CredentialSubjectRef,
)
from dotmac_kernel.models import AuthSession, PartyPerson, PartyType, UserCredential
from dotmac_kernel.transactions import conflict_savepoint, conflict_savepoint_requires_rollback
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.identity_extensions import AcademyAccountProfile, AcademyCredentialPolicy
from app.services.authentication_policy import authentication_policy
from app.services.identity import canonical_party_for_email, sync_canonical_credential_emails
from app.services.security import hash_token, issue_access_token


@dataclass(frozen=True, slots=True)
class AcademySignInAccount:
    id: UUID
    tenant_id: UUID
    email: str
    first_name: str
    last_name: str
    status: str


@dataclass(frozen=True, slots=True)
class AcademySignInResult:
    account: AcademySignInAccount
    session_id: UUID
    session_token: str = field(repr=False)


class _Unsupported:
    def _refuse(self, *args, **kwargs):
        raise CredentialLifecycleError("operation is outside canonical sign-in")

    request_revocation = _refuse
    emit = _refuse
    consume = _refuse
    record_event = _refuse
    record_receipt = _refuse
    find_receipt = _refuse
    validate = _refuse


class _VerificationStore:
    def __init__(self, snapshot: CredentialSnapshot):
        self._snapshot = snapshot

    def load(self, subject: CredentialSubjectRef) -> CredentialSnapshot:
        if subject != self._snapshot.subject:
            raise CredentialLifecycleError("verification subject binding differs")
        return self._snapshot

    def _refuse(self, *args, **kwargs):
        raise CredentialLifecycleError("credential writes are outside sign-in")

    lock_for_update = _refuse
    create = _refuse
    write_hash = _refuse


def _sign_in_locked(db: Session, *, tenant_id: UUID, email: str, password: str) -> AcademySignInResult | None:
    policy = authentication_policy(db)
    party = canonical_party_for_email(db, tenant_id=tenant_id, email=email, lock=True)
    if party is None:
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
    if person is None or profile is None or party.party_type != PartyType.person:
        return None
    # No ORDER BY: preserve the original unordered credential selector.
    credential = db.scalars(
        select(UserCredential)
        .where(UserCredential.tenant_id == tenant_id, UserCredential.party_id == party.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if credential is None:
        return None
    state = db.scalars(
        select(AcademyCredentialPolicy)
        .where(AcademyCredentialPolicy.tenant_id == tenant_id, AcademyCredentialPolicy.credential_id == credential.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if state is None:
        return None
    if (
        type(state.lifecycle_version) is not int
        or not 0 <= state.lifecycle_version <= 9223372036854775807
        or type(state.reset_required) is not bool
        or type(party.is_active) is not bool
        or type(state.failed_login_attempts) is not int
        or type(profile.status) is not str
    ):
        return None
    now = datetime.now(UTC)
    # Recheck the actual post-lock clock before verification or mutation. A
    # wait can consume the last representable portion of a configured duration.
    lockout_deadline = policy.lockout_until(now)
    # Persisted lockout precedes any owner verification, preserving legacy work/order.
    if state.locked_until is not None and state.locked_until > now:
        return None
    subject = CredentialSubjectRef("dotmac_academy_app", "person", f"{tenant_id}:{party.id}", str(credential.id))
    snapshot = CredentialSnapshot(
        subject=subject,
        password_hash=credential.password_hash,
        credential_version=state.lifecycle_version,
        is_active=party.is_active,
        is_locked=state.locked_until is not None and state.locked_until > now,
        reset_required=state.reset_required,
    )
    unsupported = _Unsupported()
    owner = CredentialLifecycle(
        store=_VerificationStore(snapshot),
        sessions=unsupported,
        recovery=unsupported,
        audit=unsupported,
        policy=unsupported,
        clock=lambda: now,
    )
    decision = owner.verify(subject, password)
    if decision.verdict == "invalid":
        state.failed_login_attempts += 1
        state.updated_at = now
        if state.failed_login_attempts >= policy.maximum_failures:
            state.locked_until = lockout_deadline
        db.flush()
        return None
    # Correct-password effects precede product suspension, as in the legacy flow.
    # Refuse incomplete policy cohorts before any owned mutation.
    sync_canonical_credential_emails(db, party=party, now=now)
    if state.failed_login_attempts != 0 or state.locked_until is not None:
        state.updated_at = now
    state.failed_login_attempts = 0
    state.locked_until = None
    db.flush()
    if not decision.accepted or profile.status != "active":
        return None
    # replacement_hash is intentionally ignored: legacy login never rewrote hashes.
    token, expires_at = issue_access_token(party.id, tenant_id)
    session = AuthSession(
        tenant_id=tenant_id,
        party_id=party.id,
        external_identity_binding_id=None,
        token_hash=hash_token(token),
        expires_at=expires_at,
    )
    db.add(session)
    db.flush()
    return AcademySignInResult(
        account=AcademySignInAccount(
            party.id, party.tenant_id, party.email, person.first_name, person.last_name, profile.status
        ),
        session_id=session.id,
        session_token=token,
    )


def sign_in(db: Session, *, tenant_id: UUID, email: str, password: str) -> AcademySignInResult | None:
    """Verify fresh canonical facts and create a session under one principal lock."""
    root = db.get_transaction()
    try:
        with conflict_savepoint(db):
            root = db.get_transaction()
            result = _sign_in_locked(db, tenant_id=tenant_id, email=email, password=password)
        return result
    except (IntegrityError, CredentialCohortDrift):
        if (
            not db.is_active
            or root is None
            or db.get_transaction() is not root
            or conflict_savepoint_requires_rollback(db, expected_root=root)
        ):
            raise CredentialLifecycleError("sign-in requires caller transaction rollback") from None
        return None
    finally:
        del password
