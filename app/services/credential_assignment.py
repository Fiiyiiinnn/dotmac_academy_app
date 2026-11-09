"""Prepared canonical web creation adapter; not activated by this source capsule.

Requires the installed successor Kernel, consumed audit action declaration,
canonical identity/session adoption and participating role/session/status writers.
This tiny return projection is not a general account reader or authority proof.
The caller owns the Session/root; every account effect belongs to one savepoint.
"""

# Ported at new-composition genesis (presentation-coupled wave).

from __future__ import annotations

import hmac
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NoReturn, cast
from uuid import UUID, uuid4

from dotmac_kernel.audit import write_audit_event
from dotmac_kernel.credential_lifecycle import (
    CredentialAssignmentAuditEntryV1,
    CredentialAssignmentAuthorityEvidenceV1,
    CredentialAssignmentError,
    CredentialCreateAbsentV1,
    CredentialLifecycle,
    CredentialPolicyViolation,
    CredentialResetPlanDigestV1,
    CredentialResetReceiptV1,
    CredentialSnapshot,
    CredentialSubjectRef,
)
from dotmac_kernel.exceptions import BadRequestError, ForbiddenError
from dotmac_kernel.models import AuthSession, Party, PartyPerson, PartyRoleGrant, PartyType, Role, UserCredential
from dotmac_kernel.transactions import conflict_savepoint, conflict_savepoint_requires_rollback
from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import require_tenant
from app.models.identity_extensions import AcademyAccountProfile, AcademyCredentialPolicy
from app.services.account_creation_policy import web_account_creation_policy
from app.services.canonical_identity_store import (
    NewPersonValues,
    RoleFact,
    create_person,
    ensure_grant,
    provision_missing_roles,
)
from app.services.credential_storage import create_absent as _create_absent_rows
from app.services.identity import normalize_email
from app.services.initial_identity_policy import InitialIdentityPolicy, initial_identity_policy
from app.services.security import hash_token
from app.services.web_auth import COOKIE

APPLICATION = "dotmac_academy_app"


@dataclass(frozen=True, slots=True)
class AcademyAccountView:
    """Canonical created principal ID needed by the existing redirect caller."""

    id: UUID


def _require_recovered_root(db, root):
    if (
        root is None
        or not db.is_active
        or db.get_transaction() is not root
        or conflict_savepoint_requires_rollback(db, expected_root=root)
    ):
        raise CredentialAssignmentError("account creation requires caller transaction rollback") from None


class _WebAssignmentPorts:
    """One private request-bound authority/store/audit/policy installation.

    Public DTO equality is never authority. Exact instance references, the actual
    Session and its original active root are checked on every installed effect.
    No password, cookie, digest or hash appears in evidence or event details.
    """

    def __init__(self, db, *, tenant_id, digest, policy):
        self.db = db
        self._bound_session = db
        self.root = db.get_transaction()
        self.tenant_id = tenant_id
        self._digest = digest
        self.policy = policy
        self._initial_identity_policy: InitialIdentityPolicy | None = None
        self.actor: Party | None = None
        self.target: Party | None = None
        self.target_role: Role | None = None
        self._target_slug = None
        self._role_catalog = {}
        self.credential_id = None
        self.command: CredentialCreateAbsentV1 | None = None
        self.evidence: CredentialAssignmentAuthorityEvidenceV1 | None = None
        self._session = None
        self._grant: PartyRoleGrant | None = None
        self._credential: UserCredential | None = None
        self._credential_policy: AcademyCredentialPolicy | None = None
        self._written = False
        self._outcome: CredentialSnapshot | None = None
        self.refusal = None

    def clear(self):
        self._initial_identity_policy = None
        self.root = None
        self._bound_session = None
        self.credential_id = None
        self._written = False
        self.evidence = None
        self.command = None
        self._digest = None
        self.actor = None
        self.target = None
        self.target_role = None
        self._target_slug = None
        self._role_catalog = {}
        self._session = None
        self._grant = None
        self._credential = None
        self._credential_policy = None
        self._outcome = None

    def _bound(self, command):
        if (
            self.db is not self._bound_session
            or not self.db.is_active
            or self.root is None
            or self.db.get_transaction() is not self.root
            or conflict_savepoint_requires_rollback(self.db, expected_root=self.root)
            or command is not self.command
            or type(command) is not CredentialCreateAbsentV1
            or self.actor is None
            or self.target is None
            or self.target_role is None
        ):
            raise CredentialAssignmentError("web assignment context refused")

    def _deny(self, category) -> NoReturn:
        self.refusal = category
        raise CredentialAssignmentError("web account creation refused")

    def _session_valid(self, session, now):
        return bool(
            session is not None
            and self.actor is not None
            and self._digest is not None
            and session.tenant_id == self.tenant_id
            and session.party_id == self.actor.id
            and session.revoked_at is None
            and session.expires_at > now
            and hmac.compare_digest(session.token_hash, self._digest)
        )

    def prove_actor_and_role(self, role):
        # Identification only. Capture IDs before refreshing/waiting; never lock
        # a session ahead of its Party, or trust this identity-map observation.
        candidate = self.db.scalars(
            select(AuthSession)
            .where(AuthSession.tenant_id == self.tenant_id, AuthSession.token_hash == self._digest)
            .execution_options(populate_existing=True)
        ).first()
        if candidate is None:
            self._deny("forbidden")
        session_id, actor_id = candidate.id, candidate.party_id
        self.actor = self.db.scalars(
            select(Party)
            .where(Party.tenant_id == self.tenant_id, Party.id == actor_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).first()
        if self.actor is None or self.actor.tenant_id != self.tenant_id or self.actor.party_type != PartyType.person:
            self._deny("forbidden")
        person = self.db.scalars(
            select(PartyPerson)
            .where(PartyPerson.party_id == actor_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).first()
        profile = self.db.scalars(
            select(AcademyAccountProfile)
            .where(AcademyAccountProfile.tenant_id == self.tenant_id, AcademyAccountProfile.party_id == actor_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).first()
        self._session = self.db.scalars(
            select(AuthSession)
            .where(AuthSession.tenant_id == self.tenant_id, AuthSession.id == session_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).first()
        # Read the clock AFTER every potentially blocking authority lock.
        if (
            self.actor is None
            or self.actor.tenant_id != self.tenant_id
            or self.actor.party_type != PartyType.person
            or self.actor.is_active is not True
            or person is None
            or person.party_id != actor_id
            or profile is None
            or profile.tenant_id != self.tenant_id
            or profile.party_id != actor_id
            or profile.status != "active"
            or not self._session_valid(self._session, datetime.now(UTC))
        ):
            self._deny("forbidden")
        roles = self.db.scalars(
            select(Role)
            .where(Role.tenant_id == self.tenant_id)
            .order_by(Role.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
        grants = self.db.scalars(
            select(PartyRoleGrant)
            .where(PartyRoleGrant.tenant_id == self.tenant_id, PartyRoleGrant.party_id == actor_id)
            .order_by(PartyRoleGrant.role_id, PartyRoleGrant.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
        by_id = {row.id: row for row in roles}
        by_slug = {row.slug: row for row in roles}
        if any(grant.role_id not in by_id for grant in grants):
            raise CredentialAssignmentError("web actor role binding differs")
        actor_roles = tuple(by_id[grant.role_id].slug for grant in grants)
        if not self.policy.can_create_any(actor_roles):
            self._deny("forbidden")
        if role not in self.policy.role_admission:
            self._deny("target")
        if not self.policy.can_assign_role(role, actor_roles):
            self._deny("forbidden")
        # Role/grant waits may cross cookie expiry. Principal/profile/session
        # remain locked, but time is not a lockable row.
        if not self._session_valid(self._session, datetime.now(UTC)):
            self._deny("forbidden")
        self._target_slug = role
        self._role_catalog = by_slug

    def create_identity(self, *, email, first_name, last_name):
        email = normalize_email(email)
        # Canonical sign-in and populated adoption require a nonblank identity
        # email. Refuse before role provisioning or any account effect; do not
        # generate a substitute address for an unusable canonical principal.
        if not email:
            self._deny("email")
        existing = self.db.scalars(
            select(Party.id).where(Party.tenant_id == self.tenant_id, func.lower(func.btrim(Party.email)) == email)
        ).first()
        if existing is not None:
            self._deny("conflict")
        # Preserve ensure_roles' actual additive behavior with exact configured
        # product names. The public Kernel permission plan is storage-neutral;
        # it does not persist Role.name. Never rename an existing tenant role.
        # The unique tenant/slug constraint preserves the old race conflict;
        # this operation does not invent retries or transaction ownership.
        now = datetime.now(UTC)
        if self._target_slug not in self._role_catalog and self._target_slug not in self.policy.role_provisioning:
            raise CredentialAssignmentError("configured target role cannot be provisioned")
        self._initial_identity_policy = initial_identity_policy(self.db)
        missing_role_ids = {
            slug: uuid4() for slug in sorted(self.policy.role_provisioning) if slug not in self._role_catalog
        }
        role_ids = tuple(row.id for row in self._role_catalog.values()) + tuple(missing_role_ids.values())
        try:
            provision_missing_roles(
                self.db,
                tenant_id=self.tenant_id,
                catalog=tuple(
                    RoleFact(row.id, row.tenant_id, row.slug, row.name) for row in self._role_catalog.values()
                ),
                configured_names=self.policy.role_provisioning,
                missing_role_ids=missing_role_ids,
                created_at=now,
                updated_at=now,
            )
        except IntegrityError as error:
            if getattr(getattr(error.orig, "diag", None), "constraint_name", None) == "uq_roles_tenant_slug":
                self.refusal = "conflict"
            raise
        # Storage facts cannot authorize or stand in for tenant-bound ORM rows.
        roles = self.db.scalars(
            select(Role)
            .where(Role.tenant_id == self.tenant_id, Role.id.in_(role_ids))
            .order_by(Role.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
        self._role_catalog = {row.slug: row for row in roles}
        self.target_role = self._role_catalog[self._target_slug]
        target_id = uuid4()
        self.credential_id = uuid4()
        try:
            create_person(
                self.db,
                values=NewPersonValues(
                    id=target_id,
                    tenant_id=self.tenant_id,
                    email=email,
                    display_name=f"{first_name} {last_name}".strip(),
                    first_name=first_name,
                    last_name=last_name,
                    is_active=self._initial_identity_policy.is_active,
                    custom_fields={},
                    profile_status=self._initial_identity_policy.profile_status,
                    profile_avatar_path=None,
                    profile_prefs={},
                    created_at=now,
                    updated_at=now,
                ),
            )
        except IntegrityError as error:
            # Typed PostgreSQL diagnostic metadata, never message text. Other
            # integrity failures (including store/audit) remain generic.
            if getattr(getattr(error.orig, "diag", None), "constraint_name", None) == "uq_parties_tenant_lower_email":
                self.refusal = "conflict"
            raise
        self.target = self.db.scalars(
            select(Party)
            .where(Party.tenant_id == self.tenant_id, Party.id == target_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one()
        ensure_grant(
            self.db,
            tenant_id=self.tenant_id,
            party_id=target_id,
            role_id=cast(Role, self.target_role).id,
            new_grant_id=uuid4(),
            created_at=now,
            updated_at=now,
        )
        self._grant = self.db.scalars(
            select(PartyRoleGrant)
            .where(
                PartyRoleGrant.tenant_id == self.tenant_id,
                PartyRoleGrant.party_id == target_id,
                PartyRoleGrant.role_id == cast(Role, self.target_role).id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one()
        self.command = self._new_command()
        return self.command

    def _new_command(self):
        return CredentialCreateAbsentV1(
            CredentialSubjectRef(APPLICATION, "person", str(cast(Party, self.target).id), str(self.credential_id)),
            str(self.tenant_id),
            "web_account_creation",
            (str(cast(Role, self.target_role).id),),
            "web_account_creation",
            str(uuid4()),
        )

    def authorize_and_lock(self, command):
        self._bound(command)
        if not self._session_valid(self._session, datetime.now(UTC)):
            self._deny("forbidden")
        if self.evidence is not None or self._written:
            raise CredentialAssignmentError("web assignment authority refused")
        if command != self._new_command_shape(command.correlation_ref):
            raise CredentialAssignmentError("web assignment envelope refused")
        self._verify_identity_and_grant()
        self.evidence = CredentialAssignmentAuthorityEvidenceV1(
            command, "user", str(cast(Party, self.actor).id), str(uuid4())
        )
        return self.evidence

    def _new_command_shape(self, correlation):
        command = self._new_command()
        return CredentialCreateAbsentV1(
            command.target_subject,
            command.tenant_ref,
            command.purpose_ref,
            command.intended_role_refs,
            command.reason_code,
            correlation,
        )

    def _verify_identity_and_grant(self):
        self.db.refresh(self.target, with_for_update=True)
        self.db.refresh(self.target_role, with_for_update=True)
        if (
            cast(Role, self.target_role).tenant_id != self.tenant_id
            or cast(Role, self.target_role).slug != self._target_slug
        ):
            raise CredentialAssignmentError("web assignment target role changed")
        self.db.refresh(self._grant, with_for_update=True)
        grants = self.db.scalars(
            select(PartyRoleGrant)
            .where(PartyRoleGrant.tenant_id == self.tenant_id, PartyRoleGrant.party_id == cast(Party, self.target).id)
            .order_by(PartyRoleGrant.role_id, PartyRoleGrant.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
        if (
            len(grants) != 1
            or grants[0].id != cast(PartyRoleGrant, self._grant).id
            or grants[0].role_id != cast(Role, self.target_role).id
        ):
            raise CredentialAssignmentError("web assignment exact role outcome differs")
        person = self.db.scalars(
            select(PartyPerson)
            .where(PartyPerson.party_id == cast(Party, self.target).id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).first()
        profile = self.db.scalars(
            select(AcademyAccountProfile)
            .where(
                AcademyAccountProfile.tenant_id == self.tenant_id,
                AcademyAccountProfile.party_id == cast(Party, self.target).id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).first()
        if (
            person is None
            or profile is None
            or self._initial_identity_policy is None
            or type(profile.status) is not str
            or profile.status != self._initial_identity_policy.profile_status
        ):
            raise CredentialAssignmentError("web assignment complete account missing")
        if (
            cast(Party, self.target).tenant_id != self.tenant_id
            or cast(Party, self.target).party_type != PartyType.person
            or cast(Party, self.target).is_active is not self._initial_identity_policy.is_active
            or cast(PartyRoleGrant, self._grant).tenant_id != self.tenant_id
            or cast(PartyRoleGrant, self._grant).party_id != cast(Party, self.target).id
            or cast(PartyRoleGrant, self._grant).role_id != cast(Role, self.target_role).id
        ):
            raise CredentialAssignmentError("web assignment target changed")

    def lock_current(self, command):
        self._bound(command)
        if self.evidence is None or self._written:
            raise CredentialAssignmentError("web assignment absence proof refused")
        rows = self.db.scalars(
            select(UserCredential)
            .where(
                UserCredential.tenant_id == self.tenant_id,
                (UserCredential.id == self.credential_id) | (UserCredential.party_id == cast(Party, self.target).id),
            )
            .order_by(UserCredential.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
        if rows:
            raise CredentialAssignmentError("web assignment requires absent credential")
        return None

    def validate(self, subject, secret):
        self._bound(self.command)
        if subject is not cast(CredentialCreateAbsentV1, self.command).target_subject:
            raise CredentialAssignmentError("web assignment policy subject refused")
        try:
            self.policy.validate(subject, secret)
        except CredentialPolicyViolation:
            self.refusal = "password"
            raise CredentialAssignmentError("configured password policy refused") from None

    def create_absent(self, command, *, password_hash, reset_required):
        self.lock_current(command)
        if reset_required is not False:
            raise CredentialAssignmentError("web assignment reset state refused")
        now = datetime.now(UTC)
        self._credential, self._credential_policy = _create_absent_rows(
            self.db,
            credential_id=self.credential_id,
            tenant_id=self.tenant_id,
            party_id=cast(Party, self.target).id,
            email=cast(Party, self.target).email,
            password_hash=password_hash,
            occurred_at=now,
        )
        self._written = True
        snapshot = self._persisted_snapshot(command)
        if snapshot.password_hash != password_hash:
            raise CredentialAssignmentError("web assignment persisted hash differs")
        self._outcome = snapshot
        return snapshot

    def _persisted_snapshot(self, command):
        self._bound(command)
        if not self._written or self._credential is None or self._credential_policy is None:
            raise CredentialAssignmentError("web assignment outcome missing")
        self._verify_identity_and_grant()
        self.db.refresh(self._credential, with_for_update=True)
        self.db.refresh(self._credential_policy, with_for_update=True)
        credential, policy = self._credential, self._credential_policy
        if (
            credential.id != self.credential_id
            or credential.tenant_id != self.tenant_id
            or credential.party_id != cast(Party, self.target).id
            or policy.credential_id != credential.id
            or policy.tenant_id != self.tenant_id
            or policy.email != cast(Party, self.target).email
            or type(policy.lifecycle_version) is not int
            or policy.lifecycle_version != 0
            or policy.reset_required is not False
            or policy.failed_login_attempts != 0
            or policy.locked_until is not None
        ):
            raise CredentialAssignmentError("web assignment persisted state differs")
        return CredentialSnapshot(
            command.target_subject,
            credential.password_hash,
            policy.lifecycle_version,
            is_active=cast(Party, self.target).is_active,
            is_locked=False,
            reset_required=policy.reset_required,
        )

    def replace_selected(self, command, *, password_hash, credential_version, reset_required):
        raise CredentialAssignmentError("replacement is outside web account creation")

    def record_assignment(self, entry: CredentialAssignmentAuditEntryV1):
        try:
            self._bound(entry.command)
            if (
                self.evidence is None
                or entry.authority is not self.evidence
                or entry.authority.command is not self.command
                or entry.authority.actor_kind != "user"
                or entry.authority.actor_ref != str(cast(Party, self.actor).id)
                or entry.previous_credential_version is not None
                or type(entry.credential_version) is not int
                or entry.credential_version != 0
            ):
                raise CredentialAssignmentError("web assignment audit authority refused")
            snapshot = self._persisted_snapshot(entry.command)
            if self._outcome is None or snapshot != self._outcome:
                raise CredentialAssignmentError("web assignment audited outcome changed")
            event = write_audit_event(
                self.db,
                tenant_id=self.tenant_id,
                actor_type="user",
                actor_id=str(cast(Party, self.actor).id),
                actor_party_id=cast(Party, self.actor).id,
                source_application=APPLICATION,
                action="credential.password.assigned",
                entity_type="user_credential",
                entity_id=str(self.credential_id),
                occurred_at=entry.occurred_at,
                is_success=True,
                details={
                    "reason_code": entry.command.reason_code,
                    "correlation_ref": entry.command.correlation_ref,
                    "authority_evidence_ref": (self.evidence.authority_evidence_ref),
                    "credential_version": entry.credential_version,
                },
            )
            self.db.flush()
            self.db.refresh(event)
            if (
                event.id is None
                or event.tenant_id != self.tenant_id
                or event.action != "credential.password.assigned"
                or event.actor_type != "user"
                or event.actor_id != str(cast(Party, self.actor).id)
                or event.actor_party_id != cast(Party, self.actor).id
                or event.entity_type != "user_credential"
                or event.entity_id != str(self.credential_id)
            ):
                raise CredentialAssignmentError("web assignment current audit differs")
            return str(event.id)
        finally:
            self.clear()

    # Old owner ports cannot accidentally implement recovery/session effects.
    def lock_for_update(self, subject: CredentialSubjectRef) -> NoReturn:
        raise CredentialAssignmentError("verification is outside web account creation")

    def create(self, subject: CredentialSubjectRef, *, password_hash: str, reset_required: bool) -> NoReturn:
        raise CredentialAssignmentError("legacy creation is outside web account creation")

    def write_hash(
        self, subject: CredentialSubjectRef, *, password_hash: str, credential_version: int, reset_required: bool
    ) -> NoReturn:
        raise CredentialAssignmentError("replacement is outside web account creation")

    def record_receipt(self, receipt: CredentialResetReceiptV1) -> NoReturn:
        raise CredentialAssignmentError("recovery audit is outside web account creation")

    def find_receipt(self, plan_digest: CredentialResetPlanDigestV1) -> NoReturn:
        raise CredentialAssignmentError("recovery audit is outside web account creation")

    def load(self, subject):
        raise CredentialAssignmentError("verification is outside web account creation")

    def record_event(self, entry):
        raise CredentialAssignmentError("recovery audit is outside web account creation")

    def emit(self, intent):
        raise CredentialAssignmentError("recovery is outside web account creation")

    def consume(self, authorization):
        raise CredentialAssignmentError("recovery is outside web account creation")

    def request_revocation(self, subject, **kwargs):
        raise CredentialAssignmentError("revocation is outside web account creation")


def create_web_user(
    db: Session, *, request: Request, email: str, first_name: str, last_name: str, password: str, role: str
) -> AcademyAccountView:
    """Create one canonical account using current request-cookie authority."""
    tenant = require_tenant(request)  # Preserve genuine product tenant semantics.
    root = db.get_transaction()
    ports = None
    expected_refusal = None
    try:
        # No Role/Party/subtype/profile/grant/credential additions before entry:
        # begin_nested may preflush caller pending work.
        with conflict_savepoint(db):
            root = db.get_transaction()
            policy = web_account_creation_policy(db)
            raw = request.cookies.get(COOKIE)
            if not raw:
                expected_refusal = "forbidden"
                raise CredentialAssignmentError("web account creation refused")
            ports = _WebAssignmentPorts(db, tenant_id=tenant.id, digest=hash_token(raw), policy=policy)
            del raw
            ports.prove_actor_and_role(role)
            command = ports.create_identity(email=email, first_name=first_name, last_name=last_name)
            view = AcademyAccountView(cast(Party, ports.target).id)
            owner = CredentialLifecycle(
                store=ports,
                sessions=ports,
                recovery=ports,
                audit=ports,
                policy=ports,
                assignment_authority=ports,
                assignment_store=ports,
                assignment_audit=ports,
            )
            owner.assign_authorized_password(command, password)
            db.flush()
        return view
    except Exception:
        _require_recovered_root(db, root)
        category = ports.refusal if ports is not None else expected_refusal
        if category == "forbidden":
            raise ForbiddenError("Forbidden") from None
        if category == "target":
            raise BadRequestError("invalid configured account role") from None
        if category == "email":
            raise BadRequestError("canonical account email is required") from None
        if category == "password":
            raise BadRequestError("password does not satisfy configured account policy") from None
        if category == "conflict":
            raise BadRequestError("account creation conflicts with existing account state") from None
        raise CredentialAssignmentError("account creation failed; no account was created") from None
    finally:
        if ports is not None:
            ports.clear()
        del password
