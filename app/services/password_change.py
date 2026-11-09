"""Prepared authenticated account-wide password change; SOURCE ONLY.

Consumes the future Kernel method-scoped account-change owner. The actual
request cookie and caller Session/root authorize self; projections/UUIDs do not.
Every membership/session/token writer must participate in the Party-first
protocol. Canonical AccountToken FK adoption and hosted qualification remain
separate gates. No service commit/rollback or external effect occurs here.
"""

# Ported at new-composition genesis (presentation-coupled wave).

from __future__ import annotations

import hmac
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, cast
from uuid import uuid4

from dotmac_kernel.audit import write_audit_event
from dotmac_kernel.credential_lifecycle import (
    CredentialAccountRef,
    CredentialAccountResetTokenInvalidationCompletionV1,
    CredentialAccountSessionRevocationCompletionV1,
    CredentialAuthenticatedAccountPasswordChangeAuthorityV1,
    CredentialAuthenticatedAccountPasswordChangeError,
    CredentialAuthenticatedAccountPasswordChangeRefused,
    CredentialAuthenticatedAccountPasswordChangeV1,
    CredentialAuthenticatedAccountPasswordPolicyRefused,
    CredentialLifecycle,
    CredentialSnapshot,
    CredentialSubjectRef,
)
from dotmac_kernel.models import AuthSession, Party, PartyPerson, PartyType, UserCredential
from dotmac_kernel.transactions import conflict_savepoint, conflict_savepoint_requires_rollback
from fastapi import HTTPException, Request
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.api.deps import require_tenant
from app.models.account_token import AccountToken
from app.models.identity_extensions import AcademyAccountProfile, AcademyCredentialPolicy
from app.services.identity import normalize_email
from app.services.password_change_policy import password_change_policy
from app.services.security import hash_token
from app.services.web_auth import COOKIE

APPLICATION = "dotmac_academy_app"


@dataclass(frozen=True, slots=True)
class PasswordChangeResult:
    code: str
    min_length: int | None = None

    @property
    def ok(self) -> bool:
        return self.code == "changed"


class _PasswordChangeRefusal(Protocol):
    code: str


class _Unauthenticated(Exception):
    pass


def _error():
    return CredentialAuthenticatedAccountPasswordChangeError("password change context refused")


def _require_recovered_root(db, root):
    if (
        root is None
        or not db.is_active
        or db.get_transaction() is not root
        or conflict_savepoint_requires_rollback(db, expected_root=root)
    ):
        raise CredentialAuthenticatedAccountPasswordChangeError("password change requires caller rollback") from None


class _DeferredPolicy:
    """Private material is checked only after the owner matched the old hash."""

    def __init__(self, ports, confirmation):
        self.ports = ports
        self.confirmation = confirmation
        self.minimum = None

    def validate(self, subject, secret):
        ports = self.ports
        if ports is None:
            raise _error()
        ports._bound()
        if subject is not ports.selected_subject or ports.authority is None:
            raise _error()
        if secret != self.confirmation:
            raise CredentialAuthenticatedAccountPasswordPolicyRefused("confirmation_mismatch")
        self.minimum = password_change_policy(ports.db).min_length
        if len(secret) < self.minimum:
            raise CredentialAuthenticatedAccountPasswordPolicyRefused("policy_rejected")

    def clear(self):
        self.confirmation = None
        self.ports = None


class _ChangePorts:
    def __init__(self, db, *, tenant_id, party_id, session_id, digest, confirmation):
        self.db = db
        self._bound_session = db
        self.root = db.get_transaction()
        self.tenant_id, self.party_id, self.session_id = tenant_id, party_id, session_id
        self.digest = digest
        self.command = CredentialAuthenticatedAccountPasswordChangeV1(
            CredentialAccountRef(APPLICATION, "person", f"{tenant_id}:{party_id}"),
            "account.password.change",
            str(uuid4()),
        )
        self.policy = _DeferredPolicy(self, confirmation)
        self.authority = None
        self.party = self.person = self.profile = self.session = None
        self.members = {}
        self.order = ()
        self.selected_subject = None
        self.initial = {}
        self.written = {}
        self._written_hashes = {}
        self.cleared = set()
        self.now = self.snapshot_at = None
        self.principal_state = None
        self.revocation_event = self.invalidation_event = None
        self.aliases_synced = False
        self.revocation = self.invalidation = None
        self.session_outcomes = {}
        self.token_outcomes = {}
        self.expected_aliases = {}
        self.expected_email = None
        self.forbidden = False

    def _bound(self, command=None):
        if (
            self.db is not self._bound_session
            or self.root is None
            or not self.db.is_active
            or self.db.get_transaction() is not self.root
            or conflict_savepoint_requires_rollback(self.db, expected_root=self.root)
            or (command is not None and command is not self.command)
        ):
            raise _error()

    def _deny(self):
        self.forbidden = True
        raise _error()

    def _account(self, account):
        self._bound()
        if account is not self.command.account:
            raise _error()

    def _authority(self, authority):
        self._bound()
        if (
            authority is not self.authority
            or self.authority is None
            or cast(CredentialAuthenticatedAccountPasswordChangeAuthorityV1, authority).command is not self.command
        ):
            raise _error()

    def _valid_self(self, now, *, revoked=False):
        return bool(
            self.party is not None
            and self.party.tenant_id == self.tenant_id
            and self.party.id == self.party_id
            and self.party.party_type == PartyType.person
            and self.party.is_active is True
            and type(self.party.email) is str
            and bool(self.party.email.strip())
            and self.person is not None
            and self.person.party_id == self.party_id
            and type(self.person.first_name) is str
            and type(self.person.last_name) is str
            and self.profile is not None
            and self.profile.tenant_id == self.tenant_id
            and self.profile.party_id == self.party_id
            and self.profile.status == "active"
            and self.session is not None
            and self.session.id == self.session_id
            and self.session.tenant_id == self.tenant_id
            and self.session.party_id == self.party_id
            and self.session.expires_at > now
            and (revoked or self.session.revoked_at is None)
            and hmac.compare_digest(self.session.token_hash, self.digest)
        )

    def _lock_principal(self):
        self.party = self.db.scalars(
            select(Party)
            .where(Party.tenant_id == self.tenant_id, Party.id == self.party_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).first()
        if self.party is None:
            self._deny()
        self.person = self.db.scalars(
            select(PartyPerson)
            .where(PartyPerson.party_id == self.party_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).first()
        self.profile = self.db.scalars(
            select(AcademyAccountProfile)
            .where(AcademyAccountProfile.tenant_id == self.tenant_id, AcademyAccountProfile.party_id == self.party_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).first()
        self.session = self.db.scalars(
            select(AuthSession)
            .where(
                AuthSession.tenant_id == self.tenant_id,
                AuthSession.id == self.session_id,
                AuthSession.party_id == self.party_id,
                AuthSession.token_hash == self.digest,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).first()

    def _rows(self):
        # Capture native unordered selector before deterministic row acquisition.
        observed = list(
            self.db.scalars(
                select(UserCredential)
                .where(UserCredential.tenant_id == self.tenant_id, UserCredential.party_id == self.party_id)
                .execution_options(populate_existing=True)
            ).all()
        )
        ids = tuple(row.id for row in observed)
        rows = list(
            self.db.scalars(
                select(UserCredential)
                .where(UserCredential.tenant_id == self.tenant_id, UserCredential.party_id == self.party_id)
                .order_by(UserCredential.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).all()
        )
        policies = list(
            self.db.scalars(
                select(AcademyCredentialPolicy)
                .where(
                    AcademyCredentialPolicy.tenant_id == self.tenant_id,
                    AcademyCredentialPolicy.credential_id.in_([row.id for row in rows]),
                )
                .order_by(AcademyCredentialPolicy.credential_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).all()
        )
        by_id = {row.id: row for row in rows}
        by_policy = {row.credential_id: row for row in policies}
        if set(ids) != set(by_id) or set(by_policy) != set(by_id):
            raise _error()
        return ids, by_id, by_policy

    def authorize_and_lock(self, command):
        self._bound(command)
        if self.authority is not None:
            raise _error()
        self._lock_principal()
        ids, credentials, policies = self._rows()
        self.now = datetime.now(UTC)  # AFTER all authority/cohort waits.
        if not self._valid_self(self.now):
            self._deny()
        self.snapshot_at = self.now
        self.principal_state = self._principal_state()
        self.order = ids
        for credential_id in ids:
            subject = CredentialSubjectRef(
                APPLICATION, "person", self.command.account.principal_ref, str(credential_id)
            )
            self.members[subject] = (credentials[credential_id], policies[credential_id])
        self.selected_subject = next(iter(self.members), None)
        self.initial = {subject: self._snapshot(subject) for subject in self.members}
        self.expected_aliases = {subject: rows[1].email for subject, rows in self.members.items()}
        self.expected_email = cast(Party, self.party).email
        self.authority = CredentialAuthenticatedAccountPasswordChangeAuthorityV1(
            command, "user", str(self.party_id), str(self.session_id), self.selected_subject
        )
        return self.authority

    def _principal_state(self):
        return (
            cast(Party, self.party).party_type,
            cast(Party, self.party).is_active,
            cast(Party, self.party).display_name,
            cast(PartyPerson, self.person).first_name,
            cast(PartyPerson, self.person).last_name,
            cast(AcademyAccountProfile, self.profile).status,
            cast(AcademyAccountProfile, self.profile).avatar_path,
            deepcopy(cast(AcademyAccountProfile, self.profile).prefs),
        )

    def _snapshot(self, subject):
        credential, policy = self.members[subject]
        if (
            credential.tenant_id != self.tenant_id
            or credential.party_id != self.party_id
            or policy.tenant_id != self.tenant_id
            or policy.credential_id != credential.id
            or type(policy.lifecycle_version) is not int
            or not 0 <= policy.lifecycle_version <= 9223372036854775807
            or type(policy.reset_required) is not bool
            or type(policy.failed_login_attempts) is not int
        ):
            raise _error()
        return CredentialSnapshot(
            subject,
            credential.password_hash,
            policy.lifecycle_version,
            is_active=cast(Party, self.party).is_active,
            is_locked=policy.locked_until is not None and policy.locked_until > self.snapshot_at,
            reset_required=policy.reset_required,
        )

    def _refresh_members(self):
        ids, credentials, policies = self._rows()
        if set(ids) != set(self.order):
            raise _error()
        for _subject, (credential, policy) in self.members.items():
            if credentials.get(credential.id) is not credential or policies.get(credential.id) is not policy:
                raise _error()

    def lock_current_credentials(self, account):
        self._account(account)
        if self.authority is None:
            raise _error()
        self._refresh_members()
        return tuple(self._snapshot(subject) for subject in self.members)

    def lock_for_update(self, subject):
        self._bound()
        if self.authority is None or subject not in self.members:
            raise _error()
        credential, policy = self.members[subject]
        self.db.refresh(credential, with_for_update=True)
        self.db.refresh(policy, with_for_update=True)
        return self._snapshot(subject)

    def revalidate_before_change(self, authority, credentials):
        self._authority(authority)
        self._lock_principal()
        self._refresh_members()
        now = datetime.now(UTC)
        if not self._valid_self(now):
            self._deny()
        # Keep snapshot lock-state semantics at the original post-acquisition
        # instant; time passing cannot invent a cohort mutation. Fresh clock is
        # used for cookie proof and persisted effect timestamps only.
        if {row.subject: row for row in credentials} != self.initial:
            return False
        if {subject: self._snapshot(subject) for subject in self.members} != self.initial:
            return False
        self.now = now
        return True

    def synchronize_account_aliases(self, authority):
        self._authority(authority)
        if self.aliases_synced:
            raise _error()
        canonical = normalize_email(cast(Party, self.party).email)
        selected = next(
            (
                subject
                for subject, (_credential, policy) in self.members.items()
                if normalize_email(policy.email) == canonical
            ),
            next(iter(self.members)),
        )
        cast(Party, self.party).email = canonical
        policy = self.members[selected][1]
        if policy.email != canonical:
            policy.email = canonical
            policy.updated_at = self.now
        self.expected_email = canonical
        self.expected_aliases[selected] = canonical
        self.db.flush()
        self.aliases_synced = True

    def write_hash(self, subject, *, password_hash, credential_version, reset_required):
        self._bound()
        if (
            not self.aliases_synced
            or subject not in self.members
            or subject in self.written
            or reset_required is not False
        ):
            raise _error()
        credential, policy = self.members[subject]
        previous = self.initial[subject].credential_version
        if credential_version != previous + 1:
            raise _error()
        result = self.db.execute(
            update(AcademyCredentialPolicy)
            .where(
                AcademyCredentialPolicy.tenant_id == self.tenant_id,
                AcademyCredentialPolicy.credential_id == credential.id,
                AcademyCredentialPolicy.lifecycle_version == previous,
            )
            .values(lifecycle_version=credential_version, reset_required=False, updated_at=self.now)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise _error()
        credential.password_hash = password_hash
        self.db.flush()
        self.db.refresh(policy)
        self.written[subject] = credential_version
        self._written_hashes[subject] = password_hash

    def clear_failure_state(self, subject):
        self._bound()
        if subject not in self.written or subject in self.cleared:
            raise _error()
        policy = self.members[subject][1]
        policy.failed_login_attempts, policy.locked_until = 0, None
        policy.updated_at = self.now
        self.db.flush()
        self.cleared.add(subject)

    def _effect_context(self, account, completed_at):
        self._account(account)
        if set(self.written) != set(self.members) or self.cleared != set(self.members) or completed_at != self.now:
            raise _error()

    def revoke_sessions(self, account, *, reason_code, correlation_ref, completed_at):
        self._effect_context(account, completed_at)
        if (
            self.revocation is not None
            or reason_code != self.command.reason_code
            or correlation_ref != self.command.correlation_ref
        ):
            raise _error()
        rows = self.db.scalars(
            select(AuthSession)
            .where(AuthSession.tenant_id == self.tenant_id, AuthSession.party_id == self.party_id)
            .order_by(AuthSession.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
        changed = 0
        for row in rows:
            if row.revoked_at is None:
                row.revoked_at = completed_at
                changed += 1
            self.session_outcomes[row.id] = (row.expires_at, row.revoked_at, row.token_hash)
        self.db.flush()
        self.revocation_event = self._stage_audit(
            "credential.sessions.revoked", "revoked_session_count", changed, completed_at
        )
        self.revocation = CredentialAccountSessionRevocationCompletionV1(
            account, str(self.revocation_event.id), completed_at, changed
        )
        return self.revocation

    def invalidate_outstanding_reset_tokens(self, account, *, completed_at):
        self._effect_context(account, completed_at)
        if self.revocation is None or self.invalidation is not None:
            raise _error()
        rows = self.db.scalars(
            select(AccountToken)
            .where(AccountToken.tenant_id == self.tenant_id, AccountToken.person_id == self.party_id)
            .order_by(AccountToken.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
        changed = 0
        for row in rows:
            if row.kind == "password_reset" and row.used_at is None:
                row.used_at = completed_at
                changed += 1
            self.token_outcomes[row.id] = (row.kind, row.expires_at, row.used_at, row.token_hash)
        self.db.flush()
        self.invalidation_event = self._stage_audit(
            "credential.reset_tokens.invalidated", "invalidated_reset_token_count", changed, completed_at
        )
        self.invalidation = CredentialAccountResetTokenInvalidationCompletionV1(
            account, str(self.invalidation_event.id), completed_at, changed
        )
        return self.invalidation

    def _stage_audit(self, action, count_key, count, completed_at):
        event = write_audit_event(
            self.db,
            tenant_id=self.tenant_id,
            actor_type="user",
            actor_id=str(self.party_id),
            actor_party_id=self.party_id,
            source_application=APPLICATION,
            action=action,
            entity_type="party",
            entity_id=str(self.party_id),
            occurred_at=completed_at,
            is_success=True,
            details={"correlation_ref": self.command.correlation_ref, count_key: count},
        )
        self.db.flush()
        self._verify_event(event, action, completed_at)
        return event

    def _verify_event(self, event, action, at):
        self.db.refresh(event)
        if (
            event.id is None
            or event.tenant_id != self.tenant_id
            or event.actor_type != "user"
            or event.actor_id != str(self.party_id)
            or event.actor_party_id != self.party_id
            or event.source_application != APPLICATION
            or event.action != action
            or event.entity_type != "party"
            or event.entity_id != str(self.party_id)
            or event.occurred_at != at
            or event.is_success is not True
        ):
            raise _error()

    def record_change(self, entry):
        self._authority(entry.authority)
        if (
            entry.command is not self.command
            or entry.revocation is not self.revocation
            or entry.invalidation is not self.invalidation
            or entry.occurred_at != self.now
            or {outcome.subject: outcome.credential_version for outcome in entry.outcomes} != self.written
            or any(
                outcome.previous_credential_version != self.initial[outcome.subject].credential_version
                for outcome in entry.outcomes
            )
        ):
            raise _error()
        self._lock_principal()
        self._refresh_members()
        # Revocation is intentional. Validate its persisted completion, not the
        # pre-write unrevoked-cookie predicate or a fresh login eligibility rule.
        if (
            not self._valid_self(self.now, revoked=True)
            or cast(Party, self.party).email != self.expected_email
            or self._principal_state() != self.principal_state
        ):
            raise _error()
        hashes = set()
        for subject, (credential, policy) in self.members.items():
            if (
                credential.password_hash != self._written_hashes[subject]
                or policy.lifecycle_version != self.written[subject]
                or policy.reset_required is not False
                or policy.failed_login_attempts != 0
                or policy.locked_until is not None
                or policy.email != self.expected_aliases[subject]
            ):
                raise _error()
            hashes.add(credential.password_hash)
        if len(hashes) != 1:
            raise _error()
        sessions = self.db.scalars(
            select(AuthSession)
            .where(AuthSession.tenant_id == self.tenant_id, AuthSession.party_id == self.party_id)
            .execution_options(populate_existing=True)
        ).all()
        tokens = self.db.scalars(
            select(AccountToken)
            .where(AccountToken.tenant_id == self.tenant_id, AccountToken.person_id == self.party_id)
            .execution_options(populate_existing=True)
        ).all()
        if {row.id: (row.expires_at, row.revoked_at, row.token_hash) for row in sessions} != self.session_outcomes:
            raise _error()
        if {row.id: (row.kind, row.expires_at, row.used_at, row.token_hash) for row in tokens} != self.token_outcomes:
            raise _error()
        for event, completion, action, key, count in (
            (
                self.revocation_event,
                self.revocation,
                "credential.sessions.revoked",
                "revoked_session_count",
                self.revocation.revoked_session_count,
            ),
            (
                self.invalidation_event,
                self.invalidation,
                "credential.reset_tokens.invalidated",
                "invalidated_reset_token_count",
                self.invalidation.invalidated_token_count,
            ),
        ):
            if event is None or completion.completion_ref != str(event.id):
                raise _error()
            self._verify_event(event, action, self.now)
            if event.details != {"correlation_ref": self.command.correlation_ref, key: count}:
                raise _error()
        details = {
            "reason_code": self.command.reason_code,
            "correlation_ref": self.command.correlation_ref,
            "session_ref": str(self.session_id),
            "credential_count": len(self.members),
            "credential_versions": [
                {
                    "credential_id": subject.credential_ref,
                    "previous_version": self.initial[subject].credential_version,
                    "version": version,
                }
                for subject, version in self.written.items()
            ],
            "session_completion_ref": self.revocation.completion_ref,
            "reset_token_completion_ref": self.invalidation.completion_ref,
            "revoked_session_count": self.revocation.revoked_session_count,
            "invalidated_reset_token_count": self.invalidation.invalidated_token_count,
        }
        event = write_audit_event(
            self.db,
            tenant_id=self.tenant_id,
            actor_type="user",
            actor_id=str(self.party_id),
            actor_party_id=self.party_id,
            source_application=APPLICATION,
            action="credential.password.changed",
            entity_type="party",
            entity_id=str(self.party_id),
            occurred_at=self.now,
            is_success=True,
            details=details,
        )
        self.db.flush()
        self._verify_event(event, "credential.password.changed", self.now)
        if event.details != details:
            raise _error()
        return str(event.id)

    def clear_context(self):
        self.policy.clear()
        self._bound_session = self.root = self.digest = self.authority = None
        self.command = cast(CredentialAuthenticatedAccountPasswordChangeV1, None)
        self.party = self.person = self.profile = self.session = None
        self.members = self.initial = self.written = self.expected_aliases = {}
        self._written_hashes = {}
        self.order = ()
        self.selected_subject = None
        self.cleared = set()
        self.session_outcomes = {}
        self.token_outcomes = {}
        self.revocation = self.invalidation = None
        self.now = self.snapshot_at = self.expected_email = self.principal_state = None
        self.revocation_event = self.invalidation_event = None
        self.db = None

    def _unsupported(self, *args, **kwargs):
        raise _error()

    load = create = request_revocation = emit = consume = record_event = record_receipt = find_receipt = _unsupported


def change_password(
    db: Session, *, request: Request, current_password: str, new_password: str, confirm_password: str
) -> PasswordChangeResult:
    """Preserve old error order and complete all effects in one public savepoint."""
    tenant = require_tenant(request)
    root = db.get_transaction()
    ports = None
    try:
        with conflict_savepoint(db):
            root = db.get_transaction()
            raw = request.cookies.get(COOKIE)
            if not raw:
                raise _Unauthenticated
            digest = hash_token(raw)
            del raw
            candidate = db.scalars(
                select(AuthSession)
                .where(AuthSession.tenant_id == tenant.id, AuthSession.token_hash == digest)
                .execution_options(populate_existing=True)
            ).first()
            if candidate is None:
                raise _Unauthenticated
            ports = _ChangePorts(
                db,
                tenant_id=tenant.id,
                party_id=candidate.party_id,
                session_id=candidate.id,
                digest=digest,
                confirmation=confirm_password,
            )
            del digest
            owner = CredentialLifecycle(
                store=ports,
                sessions=ports,
                recovery=ports,
                audit=ports,
                policy=ports.policy,
                clock=lambda: cast(datetime, ports.now),
            )
            owner.change_authenticated_account_password(ports.command, current_password, new_password, account=ports)
            db.flush()
        return PasswordChangeResult("changed")
    except Exception as exc:
        _require_recovered_root(db, root)
        if isinstance(exc, _Unauthenticated) or (ports is not None and ports.forbidden):
            raise HTTPException(status_code=303, headers={"Location": "/login"}) from None
        if type(exc) is CredentialAuthenticatedAccountPasswordChangeRefused:
            return PasswordChangeResult(
                cast(_PasswordChangeRefusal, exc).code, ports.policy.minimum if ports is not None else None
            )
        raise CredentialAuthenticatedAccountPasswordChangeError("password change failed; no change was saved") from None
    finally:
        if ports is not None:
            ports.clear_context()
        del current_password, new_password, confirm_password
