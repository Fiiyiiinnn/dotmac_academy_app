"""Detached Academy API inventory facts; no authority or table ownership."""

# Ported at new-composition genesis (learning-facing wave).

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from dotmac_kernel.audit import AuditEvent
from dotmac_kernel.models import Party, PartyPerson, PartyType
from sqlalchemy import func, select

from app.models.identity_extensions import AcademyAccountProfile


@dataclass(frozen=True, slots=True)
class ApiPersonView:
    id: UUID
    email: str | None
    first_name: str
    last_name: str


@dataclass(frozen=True, slots=True)
class ApiAuditView:
    id: UUID
    actor_person_id: UUID | None
    action: str
    entity_type: str
    entity_id: str | None
    details: object
    created_at: datetime | None


def _people(tenant_id):
    return (
        select(Party, PartyPerson)
        .join(PartyPerson, PartyPerson.party_id == Party.id)
        .join(
            AcademyAccountProfile,
            (AcademyAccountProfile.party_id == Party.id) & (AcademyAccountProfile.tenant_id == Party.tenant_id),
        )
        .where(
            Party.tenant_id == tenant_id,
            Party.party_type == PartyType.person,
            AcademyAccountProfile.tenant_id == tenant_id,
        )
        .execution_options(populate_existing=True)
    )


def _person(row):
    party, person = row
    return ApiPersonView(party.id, party.email, person.first_name, person.last_name)


def list_people(db, *, tenant_id):
    return [_person(row) for row in db.execute(_people(tenant_id).order_by(Party.created_at.desc())).all()]


def get_person(db, *, tenant_id, person_id):
    row = db.execute(_people(tenant_id).where(Party.id == person_id)).first()
    return None if row is None else _person(row)


def _audit_event_select(tenant_id, *, action=None, actor_party_id=None):
    """One canonical audit selection for API and web consumers."""
    stmt = select(AuditEvent).where(AuditEvent.tenant_id == tenant_id)
    if action:
        stmt = stmt.where(AuditEvent.action == action)
    if actor_party_id is not None:
        stmt = stmt.where(AuditEvent.actor_party_id == actor_party_id)
    return stmt.execution_options(populate_existing=True)


def _audit_rows(db, *, tenant_id, action=None, actor_party_id=None, limit=None, offset=0):
    stmt = _audit_event_select(tenant_id, action=action, actor_party_id=actor_party_id)
    stmt = stmt.order_by(AuditEvent.created_at.desc())
    if limit is not None:
        stmt = stmt.limit(limit)
    if offset:
        stmt = stmt.offset(offset)
    return db.scalars(stmt).all()


def count_audit_events(db, *, tenant_id, action=None, actor_party_id=None):
    stmt = _audit_event_select(tenant_id, action=action, actor_party_id=actor_party_id)
    return int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)


@dataclass(frozen=True, slots=True)
class WebAuditView:
    id: UUID
    actor_type: str | None
    actor_id: str | None
    actor_label: str | None
    actor_party_id: UUID | None
    action: str
    entity_type: str
    entity_id: str | None
    details: object
    created_at: datetime | None

    @property
    def actor_person_id(self):
        """Legacy detached presentation field; never an ORM identity alias."""
        return self.actor_party_id


def list_web_audit_events(db, *, tenant_id, action=None, actor_party_id=None, limit=100, offset=0):
    return [
        WebAuditView(
            row.id,
            row.actor_type,
            row.actor_id,
            row.actor_label,
            row.actor_party_id,
            row.action,
            row.entity_type,
            row.entity_id,
            deepcopy(row.details),
            row.created_at,
        )
        for row in _audit_rows(
            db, tenant_id=tenant_id, action=action, actor_party_id=actor_party_id, limit=limit, offset=offset
        )
    ]


def existing_people(db, *, tenant_id, email=None, person_ids=None):
    stmt = _people(tenant_id)
    if email is not None:
        stmt = stmt.where(Party.email == email)
    if person_ids is not None:
        stmt = stmt.where(Party.id.in_(person_ids))
    return [_person(row) for row in db.execute(stmt).all()]


def list_audit_events(db, *, tenant_id):
    rows = _audit_rows(db, tenant_id=tenant_id)
    return [
        ApiAuditView(
            row.id,
            row.actor_party_id,
            row.action,
            row.entity_type,
            row.entity_id,
            deepcopy(row.details),
            row.created_at,
        )
        for row in rows
    ]


def certificate_recipient(db, *, tenant_id, person_id) -> ApiPersonView | None:
    """Historical delivery contact, including inactive people without a profile."""
    row = db.execute(
        select(Party, PartyPerson)
        .join(PartyPerson, PartyPerson.party_id == Party.id)
        .where(Party.tenant_id == tenant_id, Party.id == person_id, Party.party_type == PartyType.person)
        .execution_options(populate_existing=True)
    ).first()
    return None if row is None else _person(row)


# Protocol identities include historical read-only baseline events. This does not
# register any writer action or establish applicant eligibility policy.
_APPLICANT_HISTORY_ACTIONS = (
    "applicant.transition",
    "applicant.transition_baseline",
    "applicant.assessment_reset",
    "applicant.assessment_reinvite",
    "applicant.invitation_resent",
    "applicant.access_extended",
    "applicant.intake_assigned",
)
_APPLICANT_INVITATION_ACTIONS = (
    "applicant.invitation_resent",
    "applicant.assessment_reinvite",
    "applicant.assessment_reset",
    "applicant.transition",
    "applicant.transition_baseline",
)


def _applicant_events(db, *, tenant_id, applicant_id, actions, newest=False):
    stmt = (
        _audit_event_select(tenant_id)
        .where(AuditEvent.entity_type == "applicant", AuditEvent.entity_id == str(applicant_id))
        .where(AuditEvent.action.in_(actions))
        .order_by(AuditEvent.created_at.desc())
    )
    if newest:
        stmt = stmt.limit(1)
    return [
        WebAuditView(
            row.id,
            row.actor_type,
            row.actor_id,
            row.actor_label,
            row.actor_party_id,
            row.action,
            row.entity_type,
            row.entity_id,
            deepcopy(row.details),
            row.created_at,
        )
        for row in db.scalars(stmt).all()
    ]


def applicant_history(db, *, tenant_id, applicant_id):
    """Full historical sequence, preserving created-at ordering including ties."""
    return _applicant_events(db, tenant_id=tenant_id, applicant_id=applicant_id, actions=_APPLICANT_HISTORY_ACTIONS)


def newest_applicant_invitation_event(db, *, tenant_id, applicant_id):
    rows = _applicant_events(
        db,
        tenant_id=tenant_id,
        applicant_id=applicant_id,
        actions=_APPLICANT_INVITATION_ACTIONS,
        newest=True,
    )
    return rows[0] if rows else None


def count_all_people(db, *, tenant_id):
    """Historical all-person count: neither activity nor product-profile gated."""
    return int(
        db.scalar(
            select(func.count())
            .select_from(Party)
            .join(
                PartyPerson,
                PartyPerson.party_id == Party.id,
            )
            .where(Party.tenant_id == tenant_id, Party.party_type == PartyType.person)
        )
        or 0
    )


def contact_selection(tenant_id):
    """Canonical historical person contact relation; no eligibility policy."""
    return (
        select(
            Party.id.label("id"),
            Party.email.label("email"),
            PartyPerson.first_name.label("first_name"),
            PartyPerson.last_name.label("last_name"),
        )
        .join(
            PartyPerson,
            PartyPerson.party_id == Party.id,
        )
        .where(Party.tenant_id == tenant_id, Party.party_type == PartyType.person)
    )


def person_contacts(db, *, tenant_id, person_ids):
    """Bounded, tenant/id-filtered batches; preserve inactive/no-profile contacts."""
    ids = tuple(dict.fromkeys(person_ids))
    result: dict[UUID, ApiPersonView] = {}
    for offset in range(0, len(ids), 500):
        rows = db.execute(contact_selection(tenant_id).where(Party.id.in_(ids[offset : offset + 500]))).all()
        result.update((row.id, ApiPersonView(row.id, row.email, row.first_name, row.last_name)) for row in rows)
    return result
