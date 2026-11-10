"""Academy's active token-authorized account-reset ports for Kernel's owner.

The public reset route uses the released account-reset API. The composed
identity migrations already persist lifecycle_version/reset_required and bind
account tokens to canonical Party/PartyPerson identities. These are current
schema facts, not future activation prerequisites.

All credential membership/session writers must participate in the canonical
Party lock protocol. The caller owns the Session and root transaction. Expected
errors become HTTP-facing domain errors only after the public savepoint exits.
"""

# Ported at new-composition genesis (learning-facing wave).

from __future__ import annotations

import hmac
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol, TypeGuard, cast
from uuid import UUID

from dotmac_kernel.audit import write_audit_event
from dotmac_kernel.credential_lifecycle import (
    CredentialAccountRecoveryAuthorizationV1,
    CredentialAccountRef,
    CredentialAccountSessionRevocationCompletionV1,
    CredentialAuditEntryV1,
    CredentialAuthorizationError,
    CredentialCohortDrift,
    CredentialLifecycle,
    CredentialLifecycleError,
    CredentialPolicyViolation,
    CredentialSnapshot,
    CredentialSubjectRef,
    PasswordPolicyPort,
)
from dotmac_kernel.models import AuthSession, Party, PartyPerson, PartyType, UserCredential
from dotmac_kernel.transactions import conflict_savepoint, conflict_savepoint_requires_rollback
from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.account_token import AccountToken
from app.models.identity_extensions import AcademyCredentialPolicy
from app.services.exceptions import BadRequestError
from app.services.identity import normalize_email
from app.services.security import hash_token

APPLICATION = "dotmac_academy_app"


def _candidate(db: Session, *, tenant_id: UUID, digest: str) -> AccountToken | None:
    # Identification only: no consumption and no token lock before Party lock.
    return db.scalars(
        select(AccountToken)
        .where(
            AccountToken.tenant_id == tenant_id,
            AccountToken.kind == "password_reset",
            AccountToken.token_hash == digest,
        )
        .execution_options(populate_existing=True)
    ).first()


class _Unsupported:
    """The account-only slice never claims another lifecycle effect succeeded."""

    def emit(self, intent):
        raise CredentialLifecycleError("recovery delivery is outside the account-reset slice")

    def consume(self, authorization):
        raise CredentialLifecycleError("subject recovery is outside the account-reset slice")

    def request_revocation(self, subject, *, reason_code, credential_version, correlation_ref):
        raise CredentialLifecycleError("lifecycle port is outside the account-reset slice")


class AccountPasswordPolicy(PasswordPolicyPort, Protocol):
    """Product policy also supplies the legacy pre-proof material check."""

    def validate_account(self, secret: str) -> None: ...


class _ResetPorts:
    def __init__(self, db: Session, *, tenant_id: UUID, raw: str, now: datetime | None):
        self.db = db
        self.tenant_id = tenant_id
        self._digest = hash_token(raw)
        self._observed_now: datetime | None = None
        self.clock: Callable[[], datetime]
        if now is None:
            self.clock = lambda: datetime.now(UTC)
        else:
            fixed_now: datetime = now
            self.clock = lambda: fixed_now
        self.party: Party | None = None
        self.token_id: UUID | None = None
        self._authorization: CredentialAccountRecoveryAuthorizationV1 | None = None
        self._consumed = False
        self._members: dict[CredentialSubjectRef, tuple[UserCredential, AcademyCredentialPolicy]] = {}

    @property
    def now(self) -> datetime:
        """Only a timestamp actually sampled after proof locks may be persisted."""
        observed = self._observed_now
        if observed is None:
            raise CredentialLifecycleError("reset clock has not been observed after proof locks")
        return observed

    def prove(self) -> CredentialAccountRecoveryAuthorizationV1:
        candidate = _candidate(self.db, tenant_id=self.tenant_id, digest=self._digest)
        if candidate is None:
            raise CredentialAuthorizationError("invalid or expired token")
        candidate_id, party_id = candidate.id, candidate.person_id
        self.party = self.db.scalars(
            select(Party)
            .where(Party.tenant_id == self.tenant_id, Party.id == party_id, Party.party_type == PartyType.person)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).first()
        if self.party is None:
            raise CredentialAuthorizationError("account no longer exists")
        # Separate statements make principal-first acquisition independent of
        # the database's join execution plan. Refresh the genuine subtype only
        # after the Party lock, matching canonical request/status/sign-in.
        person = self.db.scalars(
            select(PartyPerson)
            .where(PartyPerson.party_id == self.party.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).first()
        if person is None:
            raise CredentialAuthorizationError("account no longer exists")
        # Refresh the actual token after obtaining the principal lock. A prior
        # identity-map snapshot is not proof against concurrent consumption.
        token = self._locked_token(candidate_id)
        # Both principal and proof locks can wait. Sample the actual clock only
        # after those waits; an explicit timestamp remains deterministic.
        self._observed_now = self.clock()
        if not self._valid(token):
            raise CredentialAuthorizationError("invalid or expired token")
        self.token_id = candidate_id
        self._authorization = CredentialAccountRecoveryAuthorizationV1(
            account=self._account(),
            recovery_ref=str(token.id),
            proven_at=self.now,
            expires_at=token.expires_at,
        )
        return self._authorization

    def _account(self) -> CredentialAccountRef:
        if self.party is None:
            raise CredentialCohortDrift("canonical principal lock is required")
        return CredentialAccountRef(APPLICATION, "person", f"{self.tenant_id}:{self.party.id}")

    def _locked_token(self, token_id: UUID) -> AccountToken | None:
        return self.db.scalars(
            select(AccountToken)
            .where(AccountToken.tenant_id == self.tenant_id, AccountToken.id == token_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).first()

    def _valid(self, token: AccountToken | None) -> TypeGuard[AccountToken]:
        return bool(
            token is not None
            and self.party is not None
            and token.tenant_id == self.tenant_id
            and token.person_id == self.party.id
            and token.kind == "password_reset"
            and token.used_at is None
            and token.expires_at >= self.now
            and hmac.compare_digest(token.token_hash, self._digest)
        )

    def consume(self, authorization: CredentialAccountRecoveryAuthorizationV1) -> bool:
        # A public DTO, UUID, or admin assertion alone cannot reach a proof.
        if self._consumed or authorization is not self._authorization or self.token_id is None:
            return False
        token = self._locked_token(self.token_id)
        # A second proof lock/owner callback must not extend the proof window.
        self._observed_now = self.clock()
        if (
            not self._valid(token)
            or authorization.account != self._account()
            or token.expires_at != authorization.expires_at
        ):
            return False
        token.used_at = self.now
        self.db.flush()
        self._consumed = True
        return True

    def lock_current_credentials(self, account: CredentialAccountRef) -> tuple[CredentialSnapshot, ...]:
        if not self._consumed or account != self._account():
            raise CredentialCohortDrift("principal proof and lock are required")
        credentials = list(
            self.db.scalars(
                select(UserCredential)
                .where(
                    UserCredential.tenant_id == self.tenant_id, UserCredential.party_id == cast(Party, self.party).id
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            ).all()
        )
        if not credentials:
            raise CredentialCohortDrift("account has no credentials")
        policies = list(
            self.db.scalars(
                select(AcademyCredentialPolicy)
                .where(
                    AcademyCredentialPolicy.tenant_id == self.tenant_id,
                    AcademyCredentialPolicy.credential_id.in_([row.id for row in credentials]),
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            ).all()
        )
        by_id = {policy.credential_id: policy for policy in policies}
        if len(by_id) != len(credentials):
            raise CredentialCohortDrift("every credential needs persisted Academy policy")
        # Preserve the original unordered credential selector/fallback, moving
        # the compatible email alias to its actual product-owned policy table.
        canonical = normalize_email(cast(Party, self.party).email)
        cast(Party, self.party).email = canonical
        selected = next(
            (row for row in credentials if normalize_email(by_id[row.id].email) == canonical), credentials[0]
        )
        by_id[selected.id].email = canonical
        try:
            self.db.flush()  # Alias conflicts refuse before policy validation/hash.
        except IntegrityError:
            raise CredentialCohortDrift("account email alias conflicts") from None
        self._members = {
            CredentialSubjectRef(account.application, account.principal_kind, account.principal_ref, str(row.id)): (
                row,
                by_id[row.id],
            )
            for row in credentials
        }
        return tuple(self._snapshot(subject, *rows) for subject, rows in self._members.items())

    def _snapshot(self, subject, credential, policy) -> CredentialSnapshot:
        if type(policy.reset_required) is not bool or type(policy.lifecycle_version) is not int:
            raise CredentialCohortDrift("explicit persisted lifecycle state is required")
        return CredentialSnapshot(
            subject=subject,
            password_hash=credential.password_hash,
            credential_version=policy.lifecycle_version,
            is_active=cast(Party, self.party).is_active,
            is_locked=policy.locked_until is not None and policy.locked_until > self.now,
            reset_required=policy.reset_required,
        )

    def lock_for_update(self, subject: CredentialSubjectRef) -> CredentialSnapshot | None:
        if subject not in self._members:
            raise CredentialCohortDrift("credential is outside the locked complete cohort")
        credential, policy = self._members[subject]
        self.db.refresh(credential, with_for_update=True)
        self.db.refresh(policy, with_for_update=True)
        if (
            credential.tenant_id != self.tenant_id
            or credential.party_id != cast(Party, self.party).id
            or policy.tenant_id != self.tenant_id
        ):
            raise CredentialCohortDrift("credential binding drifted")
        return self._snapshot(subject, credential, policy)

    def write_hash(
        self, subject: CredentialSubjectRef, *, password_hash: str, credential_version: int, reset_required: bool
    ) -> None:
        if subject not in self._members or reset_required is not False:
            raise CredentialCohortDrift("reset write is outside the locked cohort")
        credential, policy = self._members[subject]
        result = self.db.execute(
            update(AcademyCredentialPolicy)
            .where(
                AcademyCredentialPolicy.tenant_id == self.tenant_id,
                AcademyCredentialPolicy.credential_id == credential.id,
                AcademyCredentialPolicy.lifecycle_version == credential_version - 1,
            )
            .values(lifecycle_version=credential_version, reset_required=False, updated_at=self.now)
            .execution_options(synchronize_session=False)
        )
        if cast(CursorResult[tuple[object, ...]], result).rowcount != 1:
            raise CredentialCohortDrift("credential lifecycle CAS refused")
        credential.password_hash = password_hash
        self.db.flush()
        self.db.refresh(policy)

    def clear_failure_state(self, subject: CredentialSubjectRef) -> None:
        _, policy = self._members[subject]
        policy.failed_login_attempts = 0
        policy.locked_until = None
        policy.updated_at = self.now
        self.db.flush()

    def revoke_sessions(
        self, account: CredentialAccountRef, *, reason_code: str, correlation_ref: str, completed_at: datetime
    ) -> CredentialAccountSessionRevocationCompletionV1:
        if account != self._account() or not self._consumed:
            raise CredentialCohortDrift("principal lock is required for revocation")
        sessions = list(
            self.db.scalars(
                select(AuthSession)
                .where(
                    AuthSession.tenant_id == self.tenant_id,
                    AuthSession.party_id == cast(Party, self.party).id,
                    AuthSession.revoked_at.is_(None),
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            ).all()
        )
        for session in sessions:
            session.revoked_at = completed_at
        self.db.flush()
        event = self._audit(
            "credential.sessions.revoked",
            "party",
            str(cast(Party, self.party).id),
            completed_at,
            {"reason_code": reason_code, "recovery_ref": correlation_ref, "revoked_session_count": len(sessions)},
        )
        return CredentialAccountSessionRevocationCompletionV1(
            account=account,
            completion_ref=str(event.id),
            completed_at=completed_at,
            revoked_session_count=len(sessions),
        )

    def _audit(self, action, entity_type, entity_id, occurred_at, details):
        return write_audit_event(
            self.db,
            tenant_id=self.tenant_id,
            actor_type="user",
            actor_id=str(cast(Party, self.party).id),
            actor_party_id=cast(Party, self.party).id,
            source_application=APPLICATION,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            occurred_at=occurred_at,
            is_success=True,
            details=details,
        )

    def record_event(self, entry: CredentialAuditEntryV1) -> None:
        if entry.subject not in self._members or entry.action != "credential.reset.completed":
            raise CredentialCohortDrift("audit subject is outside this reset")
        self._audit(
            entry.action,
            "user_credential",
            entry.subject.credential_ref,
            entry.occurred_at,
            {
                "reason_code": entry.reason_code,
                "recovery_ref": entry.correlation_ref,
                "credential_version": entry.credential_version,
            },
        )

    def invalidate_siblings(self) -> None:
        tokens = self.db.scalars(
            select(AccountToken)
            .where(
                AccountToken.tenant_id == self.tenant_id,
                AccountToken.person_id == cast(Party, self.party).id,
                AccountToken.kind == "password_reset",
                AccountToken.used_at.is_(None),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
        for token in tokens:
            token.used_at = self.now
        self.db.flush()

    def load(self, subject):
        raise CredentialLifecycleError("verification is outside this reset slice")

    def create(self, subject, *, password_hash, reset_required):
        raise CredentialLifecycleError("provisioning is outside this reset slice")

    def record_receipt(self, receipt):
        raise CredentialLifecycleError("cohort force reset is outside this reset slice")

    def find_receipt(self, plan_digest):
        raise CredentialLifecycleError("cohort force reset is outside this reset slice")


def complete_password_reset(
    db: Session,
    *,
    tenant_id: UUID,
    raw: str,
    new_password: str,
    password_policy: AccountPasswordPolicy,
    now: datetime | None = None,
) -> Party:
    """Consume genuine Academy recovery proof through the Kernel account owner."""
    try:
        password_policy.validate_account(new_password)
    except CredentialPolicyViolation:
        # A composed policy has seen material; never echo its exception text.
        raise BadRequestError("password does not satisfy configured account policy") from None
    except Exception:
        raise CredentialLifecycleError("account password policy failed; caller must roll back") from None
    root = db.get_transaction()
    ports: _ResetPorts | None = None
    try:
        with conflict_savepoint(db):
            root = db.get_transaction()
            ports = _ResetPorts(db, tenant_id=tenant_id, raw=raw, now=now)
            authorization = ports.prove()
            unsupported = _Unsupported()
            owner = CredentialLifecycle(
                store=ports,
                sessions=unsupported,
                recovery=unsupported,
                audit=ports,
                policy=password_policy,
                clock=ports.clock,
            )
            owner.complete_account_reset(authorization, new_password, account=ports)
            ports.invalidate_siblings()
            db.flush()
            if ports.party is None:
                raise CredentialCohortDrift("canonical reset principal is missing")
            party = ports.party
        return party
    except (CredentialAuthorizationError, CredentialCohortDrift, CredentialPolicyViolation, IntegrityError):
        # The route catches BadRequestError while get_db still owns the root.
        # Never turn failed savepoint recovery into a normal response/commit.
        if (
            not db.is_active
            or root is None
            or db.get_transaction() is not root
            or conflict_savepoint_requires_rollback(db, expected_root=root)
        ):
            raise CredentialLifecycleError("reset requires caller transaction rollback") from None
        # The savepoint rolled the database back, but write_hash refreshed the policy
        # to the bumped version after a Core UPDATE, which the rollback does not
        # expire. Drop the cohort's loaded state so the session re-reads the
        # committed values instead of a version the database no longer has.
        if ports is not None:
            for credential, policy in ports._members.values():
                db.expire(credential)
                db.expire(policy)
        raise BadRequestError("invalid or expired token or account reset conflict") from None
    finally:
        del new_password
        del raw
