"""Prepared human invitation application boundary; SOURCE ONLY.

The context manager encloses genuine identity/role/Learning/token/audit and
DURABLE outbox enqueue in one public savepoint. It never sends provider HTTP.
The separate onboarding operation proves its actual persisted bearer. Legacy
CLI/stranded callers and earlier admissions audit sources remain unported; a DB
handle or caller actor boolean is not authority.
"""

# Ported at new-composition genesis (presentation-coupled wave).

from __future__ import annotations

import hmac
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import dotmac_learning as learning
from dotmac_kernel.audit import write_audit_event
from dotmac_kernel.identity import person_display_name
from dotmac_kernel.models import AuthSession, Party, PartyPerson, PartyRoleGrant, PartyType, Role, UserCredential
from dotmac_kernel.transactions import conflict_savepoint, conflict_savepoint_requires_rollback
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import require_tenant
from app.invitation_declarations import INVITATION_COMPLETED_ACTION
from app.models.account_token import AccountToken
from app.models.admissions import Applicant
from app.models.identity_extensions import AcademyAccountProfile, AcademyCredentialPolicy
from app.services.account_invitations import CohortAssignment, InvitationAccountView, _invite_locked
from app.services.canonical_identity_store import (
    GrantFact,
    NewPersonValues,
    RoleFact,
    create_person,
    ensure_grant,
    provision_missing_roles,
)
from app.services.exceptions import BadRequestError, ConflictError, NotFoundError
from app.services.identity import normalize_email
from app.services.initial_identity_policy import InitialIdentityPolicy, initial_identity_policy
from app.services.invitation_policy import InvitationIssuancePolicy, invitation_issuance_policy
from app.services.security import hash_token
from app.services.web_auth import COOKIE


class InvitationIssuanceError(Exception):
    """Unexpected operation failure; never contains provider/token material."""


class _Denied(Exception):
    def __init__(self, code):
        self.code = code


@dataclass(frozen=True, slots=True)
class _Command:
    purpose: str
    email: str
    first_name: str
    last_name: str
    role: str
    assignments: tuple[CohortAssignment, ...]
    target_id: UUID | None
    ttl_hours: int | float | None


def _require_recovered_root(db, root):
    if (
        root is None
        or not db.is_active
        or db.get_transaction() is not root
        or conflict_savepoint_requires_rollback(db, expected_root=root)
    ):
        raise InvitationIssuanceError("invitation requires caller transaction rollback") from None


def cohort_fact(db, *, tenant_id, cohort_id, active_only=False):
    return next(
        (
            row
            for row in learning.instructor_cohort_choices(db, tenant_id=tenant_id)
            if row.id == cohort_id and (not active_only or row.status == "active")
        ),
        None,
    )


def course_fact(db, *, tenant_id, course_id):
    return next((row for row in learning.all_courses(db, tenant_id=tenant_id) if row.id == course_id), None)


def track_fact(db, *, tenant_id, cohort_id, track_id):
    learning.cohort_track_or_404(db, tenant_id=tenant_id, cohort_id=cohort_id, track_id=track_id)
    return next(
        (
            item.track
            for item in learning.list_cohort_tracks(db, tenant_id=tenant_id, cohort_id=cohort_id)
            if item.track.id == track_id
        ),
        None,
    )


def _issue_invitation_token(context, *, ttl_hours=None):
    if type(context) not in {_Context, _OnboardingContext}:
        raise InvitationIssuanceError("invitation issuer context differs")
    context.require()
    expiry = context.policy.deadline(context.now, ttl_hours=ttl_hours)
    if context.policy.replace_outstanding:
        for row in context.invites:
            if row.used_at is None:
                row.used_at = context.now
    context.issued_at, context.expected_expiry = context.now, expiry
    raw = secrets.token_hex(32)  # Fixed security protocol: 32 bytes / HMAC64.
    try:
        context.token_digest = hash_token(raw)
        context.token_row = AccountToken(
            tenant_id=context.tenant_id,
            person_id=context.target_id,
            kind="invite",
            token_hash=context.token_digest,
            expires_at=expiry,
        )
        context.db.add(context.token_row)
        context.db.flush()
        return raw
    finally:
        del raw


class _Context:
    def __init__(self, db, *, tenant_id, digest, session_id, actor_id, command):
        self.db = self.bound_session = db
        self.root = db.get_transaction()
        self.tenant_id, self.digest, self.session_id, self.actor_id = tenant_id, digest, session_id, actor_id
        self.command = self._issued_command = command
        self.evidence = self._issued_evidence = None
        self.party_rows = {}
        self.person_rows = {}
        self.profile_rows = {}
        self.actor_roles: frozenset[str] = frozenset()
        self.roles = {}
        self.grants = []
        self.expected_target_grants = {}
        self.session = None
        self.target: Party | None = None
        self.target_id: UUID | None = None
        self.credentials = []
        self.credential_policies = {}
        self.invites = []
        self.initial_invites = {}
        self.initial_target = None
        self.token_row = self.token_digest = self.issued_at = self.expected_expiry = None
        self.initial_credentials = {}
        self.now: datetime | None = None
        self.policy: InvitationIssuancePolicy | None = None
        self._initial_identity_policy: InitialIdentityPolicy | None = None
        self.expected_email = None
        self.expected_aliases = {}
        self.correlation: str | None = str(uuid4())

    def require(self, command=None):
        if (
            self.db is not self.bound_session
            or self.root is None
            or not self.db.is_active
            or self.db.get_transaction() is not self.root
            or conflict_savepoint_requires_rollback(self.db, expected_root=self.root)
            or self.evidence is None
            or self.evidence is not self._issued_evidence
            or self.command is not self._issued_command
            or (command is not None and command is not self.command)
        ):
            raise InvitationIssuanceError("invitation private context refused")

    def require_assignment(self, role, assignment):
        self.require()
        if role != self.command.role or assignment not in self.command.assignments:
            raise InvitationIssuanceError("invitation assignment differs from issued command")

    @property
    def actor_is_admin(self):
        self.require()
        return "admin" in self.actor_roles

    def _lookup_target(self):
        command = self.command
        query = select(Party).where(Party.tenant_id == self.tenant_id)
        if command.purpose == "resend":
            query = query.where(Party.id == command.target_id)
        else:
            query = query.where(func.lower(func.btrim(Party.email)) == normalize_email(command.email))
        return self.db.scalars(query.execution_options(populate_existing=True)).first()

    def prove(self):
        candidate = self._lookup_target()
        candidate_id = candidate.id if candidate is not None else None
        # All EXISTING principals in one deterministic order, before any
        # subtype/profile/grant/session/membership lock. Self-target deduplicates.
        ids = sorted({self.actor_id, *([candidate_id] if candidate_id is not None else [])})
        for party_id in ids:
            row = self.db.scalars(
                select(Party)
                .where(Party.tenant_id == self.tenant_id, Party.id == party_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).first()
            if row is None:
                raise _Denied("login" if party_id == self.actor_id else "target_changed")
            self.party_rows[party_id] = row
        current = self._lookup_target()
        if (current.id if current is not None else None) != candidate_id:
            raise _Denied("target_changed")  # Never acquire an out-of-order replacement Party lock.
        self.target_id = candidate_id
        for party_id in ids:
            self.person_rows[party_id] = self.db.scalars(
                select(PartyPerson)
                .where(PartyPerson.party_id == party_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).first()
            self.profile_rows[party_id] = self.db.scalars(
                select(AcademyAccountProfile)
                .where(AcademyAccountProfile.tenant_id == self.tenant_id, AcademyAccountProfile.party_id == party_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).first()
        role_rows = self.db.scalars(
            select(Role)
            .where(Role.tenant_id == self.tenant_id)
            .order_by(Role.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
        self.roles = {row.slug: row for row in role_rows}
        self.grants = list(
            self.db.scalars(
                select(PartyRoleGrant)
                .where(PartyRoleGrant.tenant_id == self.tenant_id, PartyRoleGrant.party_id.in_(ids))
                .order_by(PartyRoleGrant.party_id, PartyRoleGrant.role_id, PartyRoleGrant.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).all()
        )
        by_id = {row.id: row.slug for row in role_rows}
        if any(row.role_id not in by_id for row in self.grants):
            raise InvitationIssuanceError("invitation role catalog binding differs")
        self.actor_roles = frozenset(by_id[row.role_id] for row in self.grants if row.party_id == self.actor_id)
        self.session = self.db.scalars(
            select(AuthSession)
            .where(
                AuthSession.tenant_id == self.tenant_id,
                AuthSession.id == self.session_id,
                AuthSession.party_id == self.actor_id,
                AuthSession.token_hash == self.digest,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).first()
        if candidate_id is not None:
            self.target = self.party_rows[candidate_id]
            self._lock_membership()
        self.now = datetime.now(UTC)
        self._check_actor()
        if self.command.purpose == "resend" and candidate_id is None:
            raise NotFoundError("User not found")
        resolved_after_wait = self._lookup_target()
        if (resolved_after_wait.id if resolved_after_wait is not None else None) != candidate_id:
            raise _Denied("target_changed")
        if candidate_id is not None:
            self._check_target()
            self.expected_target_grants = {row.id: row.role_id for row in self.grants if row.party_id == self.target_id}
            self.initial_target = self._target_state()
            self.expected_email = cast(Party, self.target).email
            self.expected_aliases = {row.credential_id: row.email for row in self.credential_policies.values()}
        if not (self.command.purpose in {"resend", "strict"} and self.credentials):
            self.policy = invitation_issuance_policy(self.db, purpose="human")
            cast(InvitationIssuancePolicy, self.policy).deadline(self.now, ttl_hours=self.command.ttl_hours)
        if self.command.purpose == "admin" and self.command.role not in {"student", "instructor", "admin"}:
            raise BadRequestError("Invalid role")
        if self.command.purpose == "cohort" and self.command.role != "student":
            raise InvitationIssuanceError("cohort invitation purpose differs")
        self.evidence = self._issued_evidence = object()  # Private instance, never exported as a capability.

    def _lock_membership(self):
        self.credentials = list(
            self.db.scalars(
                select(UserCredential)
                .where(UserCredential.tenant_id == self.tenant_id, UserCredential.party_id == self.target_id)
                .order_by(UserCredential.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).all()
        )
        policies = self.db.scalars(
            select(AcademyCredentialPolicy)
            .where(
                AcademyCredentialPolicy.tenant_id == self.tenant_id,
                AcademyCredentialPolicy.credential_id.in_([row.id for row in self.credentials]),
            )
            .order_by(AcademyCredentialPolicy.credential_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
        self.credential_policies = {row.credential_id: row for row in policies}
        if set(self.credential_policies) != {row.id for row in self.credentials}:
            raise InvitationIssuanceError("complete persisted invitation alias policy is required")
        self.invites = list(
            self.db.scalars(
                select(AccountToken)
                .where(
                    AccountToken.tenant_id == self.tenant_id,
                    AccountToken.person_id == self.target_id,
                    AccountToken.kind == "invite",
                )
                .order_by(AccountToken.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).all()
        )
        self.initial_credentials = self._credential_state()
        self.initial_invites = {row.id: (row.used_at, row.expires_at, row.token_hash) for row in self.invites}

    def _credential_state(self):
        return {
            row.id: (
                row.password_hash,
                self.credential_policies[row.id].lifecycle_version,
                self.credential_policies[row.id].failed_login_attempts,
                self.credential_policies[row.id].locked_until,
                self.credential_policies[row.id].reset_required,
            )
            for row in self.credentials
        }

    def _check_actor(self):
        actor = self.party_rows.get(self.actor_id)
        person = self.person_rows.get(self.actor_id)
        profile = self.profile_rows.get(self.actor_id)
        if (
            actor is None
            or actor.id != self.actor_id
            or actor.tenant_id != self.tenant_id
            or actor.party_type != PartyType.person
            or actor.is_active is not True
            or person is None
            or person.party_id != self.actor_id
            or profile is None
            or profile.party_id != self.actor_id
            or profile.tenant_id != self.tenant_id
            or profile.status != "active"
            or type(actor.email) is not str
            or not actor.email.strip()
            or type(person.first_name) is not str
            or type(person.last_name) is not str
            or self.session is None
            or self.session.id != self.session_id
            or self.session.revoked_at is not None
            or self.session.expires_at <= self.now
            or self.session.party_id != self.actor_id
            or self.session.tenant_id != self.tenant_id
            or not hmac.compare_digest(self.session.token_hash, self.digest)
        ):
            raise _Denied("login")
        required = {"instructor", "admin"} if self.command.purpose == "cohort" else {"admin"}
        if not required.intersection(self.actor_roles):
            raise _Denied("role")

    def _check_target(self):
        person = self.person_rows.get(self.target_id)
        profile = self.profile_rows.get(self.target_id)
        if (
            self.target is None
            or self.target.id != self.target_id
            or self.target.tenant_id != self.tenant_id
            or self.target.party_type != PartyType.person
            or person is None
            or type(person.first_name) is not str
            or type(person.last_name) is not str
            or profile is None
            or profile.tenant_id != self.tenant_id
            or profile.party_id != self.target_id
        ):
            raise InvitationIssuanceError("invitation target account is incomplete")
        if self._initial_identity_policy is not None and (
            self.target.is_active is not self._initial_identity_policy.is_active
            or type(profile.status) is not str
            or profile.status != self._initial_identity_policy.profile_status
        ):
            raise InvitationIssuanceError("invitation initial identity state differs")

    def _target_state(self):
        person, profile = self.person_rows[self.target_id], self.profile_rows[self.target_id]
        return (
            cast(Party, self.target).display_name,
            cast(Party, self.target).is_active,
            person.first_name,
            person.last_name,
            profile.status,
            profile.avatar_path,
            deepcopy(profile.prefs),
        )

    def revalidate(self):
        self.require()
        # Refresh only principals already held (or newly created locally). No
        # target resolution retry can introduce an out-of-order existing lock.
        for party_id in sorted(self.party_rows):
            self.db.refresh(self.party_rows[party_id], with_for_update=True)
            self.db.refresh(self.person_rows[party_id], with_for_update=True)
            self.db.refresh(self.profile_rows[party_id], with_for_update=True)
        self.db.refresh(self.session, with_for_update=True)
        roles = self.db.scalars(
            select(Role)
            .where(Role.tenant_id == self.tenant_id)
            .order_by(Role.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
        grants = self.db.scalars(
            select(PartyRoleGrant)
            .where(PartyRoleGrant.tenant_id == self.tenant_id, PartyRoleGrant.party_id == self.actor_id)
            .order_by(PartyRoleGrant.role_id, PartyRoleGrant.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
        by_id = {row.id: row.slug for row in roles}
        if any(row.role_id not in by_id for row in grants):
            raise InvitationIssuanceError("invitation actor grant drift")
        self.actor_roles = frozenset(by_id[row.role_id] for row in grants)
        self.now = datetime.now(UTC)  # AFTER every blocking refresh/Learning wait.
        self._check_actor()
        self._check_target()
        if self._target_state() != self.initial_target:
            raise InvitationIssuanceError("invitation changed unrelated target state")
        if self._lookup_target() is not self.target:
            raise _Denied("target_changed")
        current_credentials = self.db.scalars(
            select(UserCredential)
            .where(UserCredential.tenant_id == self.tenant_id, UserCredential.party_id == self.target_id)
            .order_by(UserCredential.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
        if {row.id for row in current_credentials} != {row.id for row in self.credentials}:
            raise InvitationIssuanceError("invitation credential membership drift")
        # A credential lock can itself wait; never authorize using the earlier clock.
        self.now = datetime.now(UTC)
        self._check_actor()

    def create_identity(self, first_name, last_name, email):
        self.require()
        display_name = person_display_name(first_name, last_name)
        if len(first_name) > 100 or len(last_name) > 100 or len(display_name) > 200:
            raise BadRequestError("name exceeds account storage limit")
        self._initial_identity_policy = initial_identity_policy(self.db)
        party_id = uuid4()
        create_person(
            self.db,
            values=NewPersonValues(
                id=party_id,
                tenant_id=self.tenant_id,
                email=email,
                display_name=display_name,
                first_name=first_name,
                last_name=last_name,
                is_active=self._initial_identity_policy.is_active,
                custom_fields={},
                profile_status=self._initial_identity_policy.profile_status,
                profile_avatar_path=None,
                profile_prefs={},
                created_at=cast(datetime, self.now),
                updated_at=cast(datetime, self.now),
            ),
        )
        # Re-resolve real rows in this Session; returned facts confer no authority.
        party = self.db.scalars(
            select(Party)
            .where(Party.tenant_id == self.tenant_id, Party.id == party_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one()
        person = self.db.scalars(
            select(PartyPerson)
            .join(Party, Party.id == PartyPerson.party_id)
            .where(Party.tenant_id == self.tenant_id, PartyPerson.party_id == party_id)
            .with_for_update(of=PartyPerson)
            .execution_options(populate_existing=True)
        ).one()
        profile = self.db.scalars(
            select(AcademyAccountProfile)
            .where(AcademyAccountProfile.tenant_id == self.tenant_id, AcademyAccountProfile.party_id == party_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one()
        self.target, self.target_id = party, party.id
        self.party_rows[party.id], self.person_rows[party.id], self.profile_rows[party.id] = party, person, profile
        self.db.refresh(party, with_for_update=True)
        self.db.refresh(profile, with_for_update=True)
        if (
            party.is_active is not self._initial_identity_policy.is_active
            or type(profile.status) is not str
            or profile.status != self._initial_identity_policy.profile_status
        ):
            raise InvitationIssuanceError("invitation initial identity state differs")
        self.initial_target = self._target_state()
        self.expected_email = email

    def synchronize_aliases(self):
        self.require()
        canonical = normalize_email(cast(Party, self.target).email)
        cast(Party, self.target).email = canonical
        if self.credentials:
            # Preserve native unordered compatible-alias/fallback selection.
            ordered = self.db.scalars(
                select(UserCredential)
                .where(UserCredential.tenant_id == self.tenant_id, UserCredential.party_id == self.target_id)
                .execution_options(populate_existing=True)
            ).all()
            selected = next(
                (row for row in ordered if normalize_email(self.credential_policies[row.id].email) == canonical),
                ordered[0],
            )
            policy = self.credential_policies[selected.id]
            if policy.email != canonical:
                policy.email = canonical
                policy.updated_at = self.now
            self.expected_aliases[selected.id] = canonical
        self.expected_email = canonical
        self.db.flush()

    def ensure_role(self, role):
        self.require()
        # Additive provisioning from existing settings; preserve custom rows and names.
        missing_role_ids = {
            slug: uuid4()
            for slug in sorted(cast(InvitationIssuancePolicy, self.policy).role_provisioning)
            if slug not in self.roles
        }
        role_ids = tuple(row.id for row in self.roles.values()) + tuple(missing_role_ids.values())
        provision_missing_roles(
            self.db,
            tenant_id=self.tenant_id,
            catalog=tuple(RoleFact(row.id, row.tenant_id, row.slug, row.name) for row in self.roles.values()),
            configured_names=cast(InvitationIssuancePolicy, self.policy).role_provisioning,
            missing_role_ids=missing_role_ids,
            created_at=cast(datetime, self.now),
            updated_at=cast(datetime, self.now),
        )
        self.roles = {
            row.slug: row
            for row in self.db.scalars(
                select(Role)
                .where(Role.tenant_id == self.tenant_id, Role.id.in_(role_ids))
                .order_by(Role.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).all()
        }
        if role not in self.roles:
            raise BadRequestError(f"invalid role: {role}")
        # UI closure is separate from strict generic existing-custom-role support.
        if self.command.purpose in {"admin", "cohort"}:
            admitted = cast(InvitationIssuancePolicy, self.policy).role_admission.get(role, ())
            if not self.actor_roles.intersection(admitted):
                raise _Denied("role")
        role_id = self.roles[role].id
        ensure_grant(
            self.db,
            tenant_id=self.tenant_id,
            party_id=cast(UUID, self.target_id),
            role_id=role_id,
            new_grant_id=None,
            created_at=None,
            updated_at=None,
        )
        grant = self.db.scalars(
            select(PartyRoleGrant)
            .where(
                PartyRoleGrant.tenant_id == self.tenant_id,
                PartyRoleGrant.party_id == self.target_id,
                PartyRoleGrant.role_id == role_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one()
        self.expected_target_grants[grant.id] = grant.role_id

    def assignment_facts(self, assignment):
        self.require()
        if type(assignment) is not CohortAssignment:
            raise BadRequestError("invitation assignment requires IDs")
        cohort = cohort_fact(self.db, tenant_id=self.tenant_id, cohort_id=assignment.cohort_id)
        if cohort is None:
            raise BadRequestError("cohort does not belong to this tenant")
        if self.command.purpose == "admin" and cohort.status != "active":
            raise BadRequestError("Selected cohort was not found")
        courses = []
        for course_id in assignment.course_ids:
            course = course_fact(self.db, tenant_id=self.tenant_id, course_id=course_id)
            if course is None:
                raise NotFoundError("Selected course was not found")
            courses.append(course)
        track = (
            track_fact(self.db, tenant_id=self.tenant_id, cohort_id=assignment.cohort_id, track_id=assignment.track_id)
            if assignment.track_id is not None
            else None
        )
        if assignment.track_id is not None and track is None:
            raise NotFoundError("Selected track was not found")
        return cohort, tuple(courses), track

    def issue_token(self, *, ttl_hours=None):
        return _issue_invitation_token(self, ttl_hours=ttl_hours)

    def view(self):
        self.require()
        self._check_target()
        person, profile = self.person_rows[self.target_id], self.profile_rows[self.target_id]
        return InvitationAccountView(
            cast(UUID, self.target_id),
            self.tenant_id,
            cast(Party, self.target).email,
            person.first_name,
            person.last_name,
            profile.status,
            cast(Party, self.target).is_active,
        )

    def audit(self, result):
        self.revalidate()
        if self.command.purpose == "resend" and result.had_credential:
            return  # Original already-password response has zero mutations.
        grants = self.db.scalars(
            select(PartyRoleGrant)
            .where(
                PartyRoleGrant.tenant_id == self.tenant_id,
                PartyRoleGrant.party_id == self.target_id,
            )
            .order_by(PartyRoleGrant.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
        if {row.id: row.role_id for row in grants} != self.expected_target_grants:
            raise InvitationIssuanceError("invitation changed unrelated target grants")
        for credential_id, alias in self.expected_aliases.items():
            self.db.refresh(self.credential_policies[credential_id], with_for_update=True)
            if self.credential_policies[credential_id].email != alias:
                raise InvitationIssuanceError("invitation alias outcome differs")
        if cast(Party, self.target).email != self.expected_email:
            raise InvitationIssuanceError("invitation email outcome differs")
        if self._credential_state() != self.initial_credentials:
            raise InvitationIssuanceError("invitation changed credential security state")
        if self.token_row is not None:
            self.db.refresh(self.token_row, with_for_update=True)
            if (
                self.token_row.tenant_id != self.tenant_id
                or self.token_row.person_id != self.target_id
                or self.token_row.kind != "invite"
                or self.token_row.used_at is not None
                or self.token_row.token_hash != self.token_digest
                or self.token_row.expires_at != self.expected_expiry
            ):
                raise InvitationIssuanceError("invitation token outcome differs")
        rows = self.db.scalars(
            select(AccountToken)
            .where(
                AccountToken.tenant_id == self.tenant_id,
                AccountToken.person_id == self.target_id,
                AccountToken.kind == "invite",
            )
            .order_by(AccountToken.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
        expected_ids = set(self.initial_invites)
        if self.token_row is not None:
            expected_ids.add(self.token_row.id)
        if {row.id for row in rows} != expected_ids:
            raise InvitationIssuanceError("invitation token membership drift")
        for row in rows:
            if row.id in self.initial_invites:
                used, expiry, digest = self.initial_invites[row.id]
                expected_used = (
                    self.issued_at
                    if (
                        self.token_row is not None
                        and cast(InvitationIssuancePolicy, self.policy).replace_outstanding
                        and used is None
                    )
                    else used
                )
                if (row.used_at, row.expires_at, row.token_hash) != (expected_used, expiry, digest):
                    raise InvitationIssuanceError("invitation sibling outcome differs")
        self.now = datetime.now(UTC)
        self._check_actor()
        details = {
            "purpose": self.command.purpose,
            "correlation_ref": self.correlation,
            "created_account": result.created_person,
            "had_credential": result.had_credential,
            "issued_invitation": result.token is not None,
            "assignment_count": len(self.command.assignments),
        }
        event = write_audit_event(
            self.db,
            tenant_id=self.tenant_id,
            actor_type="user",
            actor_id=str(self.actor_id),
            actor_party_id=self.actor_id,
            source_application="dotmac_academy_app",
            action=INVITATION_COMPLETED_ACTION,
            entity_type="party",
            entity_id=str(self.target_id),
            occurred_at=self.now,
            is_success=True,
            details=details,
        )
        self.db.flush()
        self.db.refresh(event)
        if (
            event.id is None
            or event.tenant_id != self.tenant_id
            or event.actor_type != "user"
            or event.actor_id != str(self.actor_id)
            or event.actor_party_id != self.actor_id
            or event.action != INVITATION_COMPLETED_ACTION
            or event.entity_id != str(self.target_id)
            or event.entity_type != "party"
            or event.source_application != "dotmac_academy_app"
            or event.occurred_at != self.now
            or event.is_success is not True
            or event.details != details
        ):
            raise InvitationIssuanceError("invitation audit outcome differs")

    def clear(self):
        self._initial_identity_policy = None
        self.evidence = self._issued_evidence = self.digest = self.command = self._issued_command = self.root = (
            self.bound_session
        ) = self.db = None
        self.party_rows = self.person_rows = self.profile_rows = self.credential_policies = {}
        self.roles = self.expected_aliases = self.initial_invites = self.initial_credentials = (
            self.expected_target_grants
        ) = {}
        self.grants = self.credentials = self.invites = []
        self.session = self.target = self.token_row = self.policy = self.initial_target = None
        self.actor_roles = frozenset()
        self.token_digest = self.issued_at = self.expected_expiry = self.expected_email = self.now = None
        self.actor_id = self.target_id = self.session_id = self.tenant_id = self.correlation = None


@contextmanager
def _human_operation(
    db,
    *,
    request,
    purpose,
    email="",
    first_name="",
    last_name="",
    role="",
    assignments=(),
    target_id=None,
    ttl_hours=None,
):
    tenant = require_tenant(request)
    root = db.get_transaction()
    context = result = raw = digest = None
    try:
        with conflict_savepoint(db):
            root = db.get_transaction()
            raw = request.cookies.get(COOKIE)
            if not raw:
                raise _Denied("login")
            try:
                digest = hash_token(raw)
            finally:
                raw = None
            session = db.scalars(
                select(AuthSession)
                .where(AuthSession.tenant_id == tenant.id, AuthSession.token_hash == digest)
                .execution_options(populate_existing=True)
            ).first()
            if session is None:
                raise _Denied("login")
            if purpose not in {"admin", "cohort", "resend", "generic", "strict"}:
                raise InvitationIssuanceError("unsupported invitation purpose")
            if purpose != "resend" and not normalize_email(email):
                raise BadRequestError("email is required")
            command = _Command(purpose, email, first_name, last_name, role, tuple(assignments), target_id, ttl_hours)
            context = _Context(
                db,
                tenant_id=tenant.id,
                digest=digest,
                session_id=session.id,
                actor_id=session.party_id,
                command=command,
            )
            digest = None
            context.prove()
            result = _invite_locked(context, command)
            try:
                yield result
            except Exception:
                raise InvitationIssuanceError("invitation delivery enqueue failed") from None
            context.audit(result)
            db.flush()
    except Exception as exc:
        _require_recovered_root(db, root)
        if isinstance(exc, _Denied):
            if exc.code == "login":
                raise HTTPException(status_code=303, headers={"Location": "/login"}) from None
            if exc.code == "target_changed":
                raise ConflictError("invitation target changed; retry from current state") from None
            raise HTTPException(status_code=403, detail="Forbidden") from None
        if isinstance(exc, (BadRequestError, ConflictError, NotFoundError, ValueError)):
            raise exc from None
        if isinstance(exc, IntegrityError):
            raise ConflictError("invitation conflicts with existing account state") from None
        raise InvitationIssuanceError("invitation failed; no change was saved") from None
    finally:
        if context is not None:
            context.clear()
        result = None
        del raw, digest


def invite_web_account(db, *, request, email, first_name, last_name, role, assignments=(), ttl_hours=None):
    return _human_operation(
        db,
        request=request,
        purpose="admin",
        email=email,
        first_name=first_name,
        last_name=last_name,
        role=role,
        assignments=assignments,
        ttl_hours=ttl_hours,
    )


def invite_web_cohort_account(db, *, request, email, first_name, last_name, cohort_id, track_id=None, ttl_hours=None):
    return _human_operation(
        db,
        request=request,
        purpose="cohort",
        email=email,
        first_name=first_name,
        last_name=last_name,
        role="student",
        assignments=(CohortAssignment(cohort_id, track_id=track_id),),
        ttl_hours=ttl_hours,
    )


def resend_web_account_invitation(db, *, request, person_id, ttl_hours=None):
    return _human_operation(db, request=request, purpose="resend", target_id=person_id, ttl_hours=ttl_hours)


def invite_user(db, *, request, email, first_name, last_name, role, ttl_hours=None):
    """Prepared strict generic human wrapper; unported lifecycle caller is gated."""
    return _human_operation(
        db,
        request=request,
        purpose="strict",
        email=email,
        first_name=first_name,
        last_name=last_name,
        role=role,
        ttl_hours=ttl_hours,
    )


@dataclass(frozen=True, slots=True)
class OnboardingCompletion:
    applicant: object
    enrolled_now: bool
    invite_raw: str | None = field(repr=False)


class _OnboardingContext:
    """Private bearer operation; facts never establish a human actor."""

    def __init__(self, db, *, tenant_id, digest, key):
        self._issued_context = self
        self.db = self.bound_session = db
        self.root = db.get_transaction()
        self.tenant_id, self.digest, self.key = tenant_id, digest, key
        self.evidence = self._issued_evidence = None
        self.applicant: Applicant | None = None
        self.tasks = []
        self.target_id: UUID | None = None
        self.identity = None
        self.role_state = self.credential_ids = None
        self.original_roles: tuple[tuple[RoleFact, ...], tuple[GrantFact, ...]]
        self.policy: InvitationIssuancePolicy | None = None
        self.invites = []
        self.initial_invites = {}
        self.token_row = self.token_digest = self.issued_at = self.expected_expiry = None
        self.audits = []
        self.now: datetime | None = None

    def require(self):
        if (
            self is not self._issued_context
            or self.db is not self.bound_session
            or self.root is None
            or not self.db.is_active
            or self.db.get_transaction() is not self.root
            or conflict_savepoint_requires_rollback(self.db, expected_root=self.root)
            or self.evidence is None
            or self.evidence is not self._issued_evidence
        ):
            raise InvitationIssuanceError("onboarding private context refused")

    def _find(self):
        from app.models.admissions import Applicant

        return self.db.scalars(
            select(Applicant)
            .where(Applicant.tenant_id == self.tenant_id, Applicant.onboarding_token_hash == self.digest)
            .execution_options(populate_existing=True)
        ).first()

    def _resolve(self, email):
        from app.services import canonical_identity_store as store

        return store.find_party_exact_email(self.db, tenant_id=self.tenant_id, email=email)

    def prove(self):
        from app.services import canonical_identity_store as store
        from app.services import onboarding

        candidate = self._find()
        if candidate is None:
            return False
        self.applicant_id, self.email, self.linked_id = candidate.id, candidate.email, candidate.person_id
        self.intake = (candidate.cohort_id, candidate.track_id)
        fact = self._resolve(self.email)
        self.identified_id = fact.id if fact else None
        ids = tuple({value for value in (self.identified_id, self.linked_id) if value is not None})
        if len(store.lock_parties(self.db, tenant_id=self.tenant_id, party_ids=ids)) != len(ids):
            raise InvitationIssuanceError("onboarding principal changed")
        self.applicant = onboarding._lock_applicant(self.db, tenant_id=self.tenant_id, applicant_id=self.applicant_id)
        self.tasks = onboarding._lock_tasks(self.db, tenant_id=self.tenant_id, applicant_id=self.applicant_id)
        self._check_bearer()
        self.evidence = self._issued_evidence = object()
        return True

    def _check_bearer(self):
        a = self.applicant
        fact = self._resolve(self.email)
        if (
            a is None
            or a.id != self.applicant_id
            or a.tenant_id != self.tenant_id
            or type(a.onboarding_token_hash) is not str
            or not hmac.compare_digest(a.onboarding_token_hash, self.digest)
            or a.email != self.email
            or a.person_id != self.linked_id
            or (a.cohort_id, a.track_id) != self.intake
            or (fact.id if fact else None) != self.identified_id
        ):
            raise InvitationIssuanceError("onboarding bearer or identity changed")

    def complete(self):
        self.require()
        self._check_bearer()
        self.now = datetime.now(UTC)
        for task in self.tasks:
            if task.key == self.key:
                task.status, task.completed_at = "done", self.now
        self.db.flush()
        self.task_state = self._task_state()
        a = cast(Applicant, self.applicant)
        self.expected_status = a.status
        if a.status != "onboarding" or a.cohort_id is None:
            return OnboardingCompletion(a, False, None)
        if not self.tasks or any(task.status != "done" for task in self.tasks):
            return OnboardingCompletion(a, False, None)
        from app.services import admissions
        from app.services import canonical_identity_store as store
        from app.services.onboarding_policy import onboarding_policy

        admissions._enroll_locked(self.db, applicant=a, cohort_id=a.cohort_id, context=self)
        self.onboarding_policy = onboarding_policy(self.db)
        self.policy = invitation_issuance_policy(self.db, purpose="onboarding")
        self.linked_id = a.person_id
        self.identified_id = a.person_id
        self.target_id = a.person_id
        self.expected_status = "enrolled"
        if self._identity_state() != self.identity:
            raise InvitationIssuanceError("onboarding identity changed")
        catalog = store.lock_roles(self.db, tenant_id=self.tenant_id)
        catalog = store.provision_missing_roles(
            self.db,
            tenant_id=self.tenant_id,
            catalog=catalog,
            configured_names=self.onboarding_policy.role_provisioning,
            missing_role_ids={slug: uuid4() for slug in self.onboarding_policy.role_provisioning},
            created_at=cast(datetime, self.now),
            updated_at=cast(datetime, self.now),
        )
        role = next((row for row in catalog if row.slug == self.onboarding_policy.global_role), None)
        if role is None:
            raise BadRequestError("configured onboarding role is unavailable")
        grant = store.ensure_grant(
            self.db,
            tenant_id=self.tenant_id,
            party_id=cast(UUID, self.target_id),
            role_id=role.id,
            new_grant_id=uuid4(),
            created_at=cast(datetime, self.now),
            updated_at=cast(datetime, self.now),
        )
        self.role_state = self._roles()
        if (
            not set(self.original_roles[0]).issubset(set(catalog))
            or set(self.role_state[0]) != set(catalog)
            or set(self.role_state[1]) != {*self.original_roles[1], grant}
        ):
            raise InvitationIssuanceError("onboarding changed unrelated roles or grants")
        self.credential_ids = store.credential_member_ids(
            self.db, tenant_id=self.tenant_id, party_id=cast(UUID, self.target_id)
        )
        self.invites = list(
            self.db.scalars(
                select(AccountToken)
                .where(
                    AccountToken.tenant_id == self.tenant_id,
                    AccountToken.person_id == self.target_id,
                    AccountToken.kind == "invite",
                )
                .order_by(AccountToken.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).all()
        )
        self.initial_invites = {row.id: (row.used_at, row.expires_at, row.token_hash) for row in self.invites}
        self.revalidate()
        self.now = datetime.now(UTC)
        self._check_bearer()
        raw = None if self.credential_ids else _issue_invitation_token(self)
        self._audit(
            INVITATION_COMPLETED_ACTION,
            "party",
            self.target_id,
            {
                "purpose": "onboarding",
                "issued_invitation": raw is not None,
                "had_credential": bool(self.credential_ids),
            },
        )
        return OnboardingCompletion(a, True, raw)

    def _task_state(self):
        return {
            row.id: (row.tenant_id, row.applicant_id, row.key, row.label, row.order_index, row.status, row.completed_at)
            for row in self.tasks
        }

    def _identity_state(self):
        from app.services import canonical_identity_store as store

        party = store.lock_parties(self.db, tenant_id=self.tenant_id, party_ids=(cast(UUID, self.target_id),))
        custom_fields = (
            self.db.scalars(
                select(Party)
                .where(Party.tenant_id == self.tenant_id, Party.id == self.target_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            .one()
            .custom_fields
        )
        return (
            party,
            store.lock_person(self.db, tenant_id=self.tenant_id, party_id=cast(UUID, self.target_id)),
            store.lock_profile(self.db, tenant_id=self.tenant_id, party_id=cast(UUID, self.target_id)),
            deepcopy(custom_fields),
        )

    def _roles(self):
        from app.services import canonical_identity_store as store

        return (
            store.lock_roles(self.db, tenant_id=self.tenant_id),
            store.lock_grants(self.db, tenant_id=self.tenant_id, party_ids=(cast(UUID, self.target_id),)),
        )

    def _audit(self, action, entity_type, entity_id, details):
        self.require()
        self._check_bearer()
        now = datetime.now(UTC)
        expected = dict(
            tenant_id=self.tenant_id,
            actor_type="system",
            actor_id="onboarding",
            actor_party_id=None,
            source_application="dotmac_academy_app",
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id),
            occurred_at=now,
            is_success=True,
            details=details,
        )
        row = write_audit_event(self.db, **expected)
        self.db.flush()
        self.audits.append((row, deepcopy(expected)))
        self._check_audits()

    def _check_audits(self):
        for row, expected in self.audits:
            self.db.refresh(row)
            if row.id is None or any(getattr(row, key) != value for key, value in expected.items()):
                raise InvitationIssuanceError("onboarding audit outcome differs")

    def revalidate(self):
        from app.services import canonical_identity_store as store
        from app.services import onboarding

        self.require()
        self.db.flush()
        self.require()
        self.db.refresh(self.applicant, with_for_update=True)
        self._check_bearer()
        self.tasks = onboarding._lock_tasks(self.db, tenant_id=self.tenant_id, applicant_id=self.applicant_id)
        if self._task_state() != self.task_state or cast(Applicant, self.applicant).status != self.expected_status:
            raise InvitationIssuanceError("onboarding task or applicant state changed")
        if self.target_id is not None:
            if self._identity_state() != self.identity or self._roles() != self.role_state:
                raise InvitationIssuanceError("onboarding identity or grant state changed")
            if (
                store.credential_member_ids(self.db, tenant_id=self.tenant_id, party_id=cast(UUID, self.target_id))
                != self.credential_ids
            ):
                raise InvitationIssuanceError("onboarding credential membership changed")
            rows = self.db.scalars(
                select(AccountToken)
                .where(
                    AccountToken.tenant_id == self.tenant_id,
                    AccountToken.person_id == self.target_id,
                    AccountToken.kind == "invite",
                )
                .order_by(AccountToken.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).all()
            expected_ids = set(self.initial_invites)
            if self.token_row is not None:
                expected_ids.add(self.token_row.id)
            if {row.id for row in rows} != expected_ids:
                raise InvitationIssuanceError("onboarding invitation membership changed")
            for row in rows:
                if self.token_row is not None and row.id == self.token_row.id:
                    expected = (None, self.expected_expiry, self.token_digest)
                else:
                    used, expiry, digest = self.initial_invites[row.id]
                    expected = (
                        self.issued_at
                        if (
                            self.token_row is not None
                            and cast(InvitationIssuancePolicy, self.policy).replace_outstanding
                            and used is None
                        )
                        else used,
                        expiry,
                        digest,
                    )
                if (row.used_at, row.expires_at, row.token_hash) != expected:
                    raise InvitationIssuanceError("onboarding invitation outcome differs")
        self._check_audits()
        self.require()

    def clear(self):
        self.__dict__.clear()


@contextmanager
def finish_onboarding_step(
    db: Session, *, tenant_id: UUID, raw: str, key: str
) -> Iterator[OnboardingCompletion | None]:
    """Complete a trusted route procedure and include durable welcome enqueue."""
    if key not in {"confirm_details", "orientation"}:
        raise BadRequestError("unsupported onboarding procedure")
    digest = hash_token(raw) if type(raw) is str and raw else None
    del raw
    root = db.get_transaction()
    context = None
    try:
        with conflict_savepoint(db):
            if digest is None:
                yield None
                return
            context = _OnboardingContext(db, tenant_id=tenant_id, digest=digest, key=key)
            if not context.prove():
                yield None
                return
            result = context.complete()
            try:
                yield result
                context.revalidate()
            finally:
                del result
    except Exception:
        _require_recovered_root(db, root)
        raise
    finally:
        digest = None
        if context is not None:
            context.clear()
        context = None
