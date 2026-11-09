"""Dormant canonical invitation claim adapter; no route is activated.

Requires the released proof-create Kernel, canonical AccountToken FKs, consumed
audit declaration and participating issuer/credential/session/status writers.
The injected account policy must be validated Settings/env policy; this module
supplies no strength default. Caller owns Session/root, including pending work.
"""

# Ported at new-composition genesis (learning-facing wave).

from __future__ import annotations

import hmac
from datetime import UTC, datetime
from typing import NoReturn, cast
from uuid import UUID, uuid4

from dotmac_kernel.audit import write_audit_event
from dotmac_kernel.credential_lifecycle import (
    CredentialAssignmentAuditEntryV1,
    CredentialAssignmentAuthorityEvidenceV1,
    CredentialAssignmentError,
    CredentialCreateAbsentFromProofV1,
    CredentialLifecycle,
    CredentialPolicyViolation,
    CredentialResetPlanDigestV1,
    CredentialResetReceiptV1,
    CredentialSnapshot,
    CredentialSubjectRef,
)
from dotmac_kernel.models import Party, PartyPerson, PartyType, UserCredential
from dotmac_kernel.transactions import conflict_savepoint, conflict_savepoint_requires_rollback
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.account_token import AccountToken
from app.models.identity_extensions import AcademyAccountProfile, AcademyCredentialPolicy
from app.services.credential_storage import create_absent as _create_absent_rows
from app.services.exceptions import BadRequestError, ConflictError
from app.services.identity import normalize_email
from app.services.security import hash_token

APPLICATION = "dotmac_academy_app"
PURPOSE = "invitation_acceptance"


class _InvitationClaimPorts:
    """Private single-operation token authority, bound to the actual root."""

    def __init__(self, db, *, tenant_id, digest, policy, clock):
        self.db = db
        self._session = db
        self.root = db.get_transaction()
        self.tenant_id = tenant_id
        self._digest = digest
        self.policy = policy
        self.clock = clock
        self.party: Party | None = None
        self.person: PartyPerson | None = None
        self.profile: AcademyAccountProfile | None = None
        self.token: AccountToken | None = None
        self.credential_id: UUID | None = uuid4()
        self.command: CredentialCreateAbsentFromProofV1 | None = None
        self.evidence: CredentialAssignmentAuthorityEvidenceV1 | None = None
        self.credential: UserCredential | None = None
        self.credential_policy: AcademyCredentialPolicy | None = None
        self._outcome: CredentialSnapshot | None = None
        self._consumed = False
        self._audited = False
        self.refusal = None

    def clear(self):
        self._session = self.root = self._digest = self.policy = None
        self.party = self.person = self.profile = self.token = None
        self.command = self.evidence = self.credential = self.credential_policy = self._outcome = None
        self.credential_id = None
        self._consumed = self._audited = False

    def _deny(self, category="token") -> NoReturn:
        self.refusal = category
        raise CredentialAssignmentError("invitation claim refused")

    def _bound(self, command):
        if (
            self.db is not self._session
            or self.root is None
            or not self.db.is_active
            or self.db.get_transaction() is not self.root
            or conflict_savepoint_requires_rollback(self.db, expected_root=self.root)
            or command is not self.command
            or type(command) is not CredentialCreateAbsentFromProofV1
            or self._digest is None
            or self.party is None
            or self.token is None
        ):
            raise CredentialAssignmentError("invitation context refused")
        if self._consumed and (
            self.token.tenant_id != self.tenant_id
            or self.token.person_id != self.party.id
            or self.token.kind != "invite"
            or self.token.used_at is None
            or not hmac.compare_digest(self.token.token_hash, self._digest)
            or self.party.tenant_id != self.tenant_id
            or self.party.party_type != PartyType.person
            or cast(PartyPerson, self.person).party_id != self.party.id
            or cast(AcademyAccountProfile, self.profile).party_id != self.party.id
            or cast(AcademyAccountProfile, self.profile).tenant_id != self.tenant_id
        ):
            raise CredentialAssignmentError("consumed invitation binding differs")
        expected = CredentialCreateAbsentFromProofV1(
            CredentialSubjectRef(APPLICATION, "person", str(self.party.id), str(self.credential_id)),
            str(self.tenant_id),
            PURPOSE,
            str(self.token.id),
            PURPOSE,
            str(self.token.id),
        )
        if command != expected:
            raise CredentialAssignmentError("invitation command binding differs")

    def _locked(self, model, *conditions):
        return self.db.scalars(
            select(model).where(*conditions).with_for_update().execution_options(populate_existing=True)
        ).first()

    def _token_valid(self, now):
        token = self.token
        return bool(
            token is not None
            and self.party is not None
            and self._digest is not None
            and token.tenant_id == self.tenant_id
            and token.person_id == self.party.id
            and token.kind == "invite"
            and token.used_at is None
            and token.expires_at >= now
            and hmac.compare_digest(token.token_hash, self._digest)
        )

    def prepare(self):
        # Identification only. Capture IDs before principal refresh; no token
        # lock can precede Party, subtype and product profile locks.
        candidate = self.db.scalars(
            select(AccountToken)
            .where(
                AccountToken.tenant_id == self.tenant_id,
                AccountToken.kind == "invite",
                AccountToken.token_hash == self._digest,
            )
            .execution_options(populate_existing=True)
        ).first()
        if candidate is None:
            self._deny()
        token_id, party_id = candidate.id, candidate.person_id
        self.party = self._locked(Party, Party.tenant_id == self.tenant_id, Party.id == party_id)
        if self.party is None or self.party.party_type != PartyType.person:
            self._deny()
        self.person = self._locked(PartyPerson, PartyPerson.party_id == party_id)
        self.profile = self._locked(
            AcademyAccountProfile,
            AcademyAccountProfile.tenant_id == self.tenant_id,
            AcademyAccountProfile.party_id == party_id,
        )
        self.token = self._locked(AccountToken, AccountToken.tenant_id == self.tenant_id, AccountToken.id == token_id)
        if self.person is None or self.profile is None:
            self._deny()
        self.command = CredentialCreateAbsentFromProofV1(
            CredentialSubjectRef(APPLICATION, "person", str(party_id), str(self.credential_id)),
            str(self.tenant_id),
            PURPOSE,
            str(token_id),
            PURPOSE,
            str(token_id),
        )
        return self.command

    def _absent(self, command, *, refuse=True):
        rows = self.db.scalars(
            select(UserCredential)
            .where(
                UserCredential.tenant_id == self.tenant_id,
                (UserCredential.party_id == cast(Party, self.party).id) | (UserCredential.id == self.credential_id),
            )
            .order_by(UserCredential.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
        if rows and refuse:
            self._deny("conflict")
        return rows

    def authorize_and_lock(self, command):
        self._bound(command)
        if self.evidence is not None or self._consumed:
            self._deny()
        rows = self._absent(command, refuse=False)  # Complete account cohort.
        now = self.clock()  # All authority/cohort waits precede expiry check.
        if not self._token_valid(now):
            self._deny()
        if rows:
            self._deny("conflict")
        cast(AccountToken, self.token).used_at = now
        self.db.flush()  # Genuine token is consumed BEFORE issuing evidence.
        self._consumed = True
        self.evidence = CredentialAssignmentAuthorityEvidenceV1(
            command, "user", str(cast(Party, self.party).id), str(cast(AccountToken, self.token).id)
        )
        return self.evidence

    def lock_current(self, command):
        self._bound(command)
        if not self._consumed or self.evidence is None or self.credential is not None:
            self._deny()
        self._absent(command)
        return None

    def validate(self, subject, secret):
        self._bound(self.command)
        if subject is not cast(CredentialCreateAbsentFromProofV1, self.command).target_subject:
            self._deny()
        try:
            self.policy.validate(subject, secret)
        except CredentialPolicyViolation:
            self._deny("password")

    def create_absent(self, command, *, password_hash, reset_required):
        self.lock_current(command)
        if reset_required is not False:
            self._deny()
        now = self.clock()
        cast(Party, self.party).email = normalize_email(cast(Party, self.party).email)
        self.credential, self.credential_policy = _create_absent_rows(
            self.db,
            credential_id=self.credential_id,
            tenant_id=self.tenant_id,
            party_id=cast(Party, self.party).id,
            email=cast(Party, self.party).email,
            password_hash=password_hash,
            occurred_at=now,
        )
        self._outcome = self._snapshot(command)
        if self._outcome.password_hash != password_hash:
            self._deny()
        return self._outcome

    def _snapshot(self, command):
        self._bound(command)
        self.db.refresh(self.credential, with_for_update=True)
        self.db.refresh(self.credential_policy, with_for_update=True)
        credential, policy = (
            cast(UserCredential, self.credential),
            cast(AcademyCredentialPolicy, self.credential_policy),
        )
        if (
            str(credential.id) != command.target_subject.credential_ref
            or credential.tenant_id != self.tenant_id
            or credential.party_id != cast(Party, self.party).id
            or policy.tenant_id != self.tenant_id
            or policy.credential_id != credential.id
            or policy.email != cast(Party, self.party).email
            or type(policy.lifecycle_version) is not int
            or policy.lifecycle_version != 0
            or policy.reset_required is not False
            or policy.failed_login_attempts != 0
            or policy.locked_until is not None
        ):
            self._deny()
        return CredentialSnapshot(
            command.target_subject,
            credential.password_hash,
            0,
            is_active=cast(Party, self.party).is_active,
            is_locked=False,
            reset_required=False,
        )

    def record_assignment(self, entry: CredentialAssignmentAuditEntryV1):
        self._bound(entry.command)
        if (
            self._audited
            or entry.authority is not self.evidence
            or not self._consumed
            or entry.authority.command is not self.command
            or entry.authority.actor_kind != "user"
            or entry.authority.actor_ref != str(cast(Party, self.party).id)
            or entry.authority.authority_evidence_ref != str(cast(AccountToken, self.token).id)
            or entry.previous_credential_version is not None
            or type(entry.credential_version) is not int
            or entry.credential_version != 0
            or self._snapshot(entry.command) != self._outcome
        ):
            self._deny()
        event = write_audit_event(
            self.db,
            tenant_id=self.tenant_id,
            actor_type="user",
            actor_id=str(cast(Party, self.party).id),
            actor_party_id=cast(Party, self.party).id,
            source_application=APPLICATION,
            action="credential.password.assigned",
            entity_type="user_credential",
            entity_id=str(cast(UserCredential, self.credential).id),
            occurred_at=entry.occurred_at,
            is_success=True,
            details={
                "reason_code": PURPOSE,
                "correlation_ref": str(cast(AccountToken, self.token).id),
                "authority_evidence_ref": str(cast(AccountToken, self.token).id),
                "credential_version": 0,
            },
        )
        self.db.flush()
        self.db.refresh(event)
        if (
            event.id is None
            or event.tenant_id != self.tenant_id
            or event.actor_type != "user"
            or event.actor_id != str(cast(Party, self.party).id)
            or event.actor_party_id != cast(Party, self.party).id
            or event.action != "credential.password.assigned"
            or event.entity_type != "user_credential"
            or event.entity_id != str(cast(UserCredential, self.credential).id)
        ):
            self._deny()
        self._audited = True
        return str(event.id)

    def invalidate_siblings(self):
        self._bound(self.command)
        if not self._audited:
            self._deny()
        tokens = self.db.scalars(
            select(AccountToken)
            .where(
                AccountToken.tenant_id == self.tenant_id,
                AccountToken.person_id == cast(Party, self.party).id,
                AccountToken.kind == "invite",
                AccountToken.used_at.is_(None),
            )
            .order_by(AccountToken.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
        now = self.clock()
        for token in tokens:
            token.used_at = now
        self.db.flush()

    def replace_selected(self, *args, **kwargs):
        raise CredentialAssignmentError("replacement is outside invitation claim")

    def lock_for_update(self, subject: CredentialSubjectRef) -> NoReturn:
        raise CredentialAssignmentError("verification is outside invitation claim")

    def create(self, subject: CredentialSubjectRef, *, password_hash: str, reset_required: bool) -> NoReturn:
        raise CredentialAssignmentError("legacy creation is outside invitation claim")

    def write_hash(
        self, subject: CredentialSubjectRef, *, password_hash: str, credential_version: int, reset_required: bool
    ) -> NoReturn:
        raise CredentialAssignmentError("replacement is outside invitation claim")

    def record_receipt(self, receipt: CredentialResetReceiptV1) -> NoReturn:
        raise CredentialAssignmentError("recovery audit is outside invitation claim")

    def find_receipt(self, plan_digest: CredentialResetPlanDigestV1) -> NoReturn:
        raise CredentialAssignmentError("recovery audit is outside invitation claim")

    def load(self, *args):
        raise CredentialAssignmentError("verification is outside invitation claim")

    def record_event(self, *args):
        raise CredentialAssignmentError("recovery audit is outside invitation claim")

    def emit(self, *args):
        raise CredentialAssignmentError("recovery is outside invitation claim")

    def consume(self, *args):
        raise CredentialAssignmentError("recovery is outside invitation claim")

    def request_revocation(self, *args, **kwargs):
        raise CredentialAssignmentError("session mutation is outside invitation claim")


def accept_invite(db: Session, *, tenant_id, raw: str, password: str, password_policy, now=None) -> Party:
    """Accept one genuine invitation; preserve roles, status and sessions."""
    # Match legacy ordering: material semantics before token mutation.
    try:
        password_policy.validate_account(password)
    except CredentialPolicyViolation:
        raise BadRequestError("password does not satisfy configured account policy") from None
    except Exception:
        raise CredentialAssignmentError("configured account policy failed") from None
    root = db.get_transaction()
    ports = None
    category = None
    try:
        with conflict_savepoint(db):
            root = db.get_transaction()
            clock = (lambda: datetime.now(UTC)) if now is None else (lambda: now)
            ports = _InvitationClaimPorts(
                db, tenant_id=tenant_id, digest=hash_token(raw), policy=password_policy, clock=clock
            )
            command = ports.prepare()
            party = cast(Party, ports.party)
            owner = CredentialLifecycle(
                store=ports,
                sessions=ports,
                recovery=ports,
                audit=ports,
                policy=ports,
                clock=clock,
                assignment_authority=ports,
                assignment_store=ports,
                assignment_audit=ports,
            )
            owner.assign_authorized_password(command, password)
            ports.invalidate_siblings()
            db.flush()
        return party
    except Exception:
        if (
            root is None
            or not db.is_active
            or db.get_transaction() is not root
            or conflict_savepoint_requires_rollback(db, expected_root=root)
        ):
            raise CredentialAssignmentError("invitation claim requires caller transaction rollback") from None
        category = ports.refusal if ports is not None else category
        if category == "conflict":
            raise ConflictError("account already has a credential") from None
        if category == "password":
            raise BadRequestError("password does not satisfy configured account policy") from None
        if category == "token":
            raise BadRequestError("invalid or expired token") from None
        raise CredentialAssignmentError("invitation claim failed; no claim effects persisted") from None
    finally:
        if ports is not None:
            ports.clear()
        del password, raw
