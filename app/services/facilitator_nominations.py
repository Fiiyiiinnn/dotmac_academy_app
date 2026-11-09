"""Authoritative facilitator nomination, eligibility, and activation lifecycle.

Facilitator access is deliberately separate from ``Enrollment``. A person can
remain an active learner with their track and progress intact while a scoped
nomination waits for its snapshotted course-completion requirements.
"""

# Ported at new-composition genesis (identity/facilitator slice).

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from dotmac_kernel.models import Party, PartyPerson, PartyType
from dotmac_learning.models import CourseCompletion
from dotmac_learning.participation_models import Cohort, CourseOffering
from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models.facilitator import FacilitatorNomination, FacilitatorRequirement
from app.models.identity_extensions import AcademyAccountProfile
from app.services.audit import write_audit_event
from app.services.exceptions import BadRequestError, NotFoundError
from app.services.notifications import notify


class FacilitatorScope(StrEnum):
    COURSE = "course"
    COHORT = "cohort"


class FacilitatorStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    REVOKED = "revoked"


@dataclass(frozen=True)
class NominateFacilitatorCommand:
    tenant_id: UUID
    person_id: UUID
    actor_person_id: UUID
    scope: FacilitatorScope
    cohort_id: UUID | None = None
    offering_id: UUID | None = None


@dataclass(frozen=True)
class ReconcileFacilitatorCommand:
    tenant_id: UUID
    nomination_id: UUID


@dataclass(frozen=True)
class ReconcileCompletedCourseCommand:
    tenant_id: UUID
    person_id: UUID
    course_id: UUID


@dataclass(frozen=True)
class RevokeFacilitatorCommand:
    tenant_id: UUID
    nomination_id: UUID
    actor_person_id: UUID


@dataclass(frozen=True)
class SweepFacilitatorsCommand:
    tenant_id: UUID
    limit: int = 100


@dataclass(frozen=True)
class FacilitatorNominationOutcome:
    nomination_id: UUID
    scope: FacilitatorScope
    status: FacilitatorStatus
    created: bool
    activated_now: bool
    required_course_ids: tuple[UUID, ...]
    missing_course_ids: tuple[UUID, ...]


@dataclass(frozen=True)
class FacilitatorSweepOutcome:
    examined: int
    activated: int
    pending: int


@dataclass(frozen=True)
class _ActivationAttempt:
    nomination_id: UUID
    activated_now: bool


def _person_or_404(db: Session, *, tenant_id: UUID, person_id: UUID) -> Party:
    person = db.scalars(
        select(Party)
        .join(PartyPerson, PartyPerson.party_id == Party.id)
        .where(Party.tenant_id == tenant_id)
        .where(Party.id == person_id)
        .where(Party.party_type == PartyType.person)
    ).first()
    if person is None:
        raise NotFoundError("person not found for tenant")
    return person


def _require_active_nominee(db: Session, *, tenant_id: UUID, person_id: UUID) -> Party:
    person = _person_or_404(db, tenant_id=tenant_id, person_id=person_id)
    status = db.scalar(
        select(AcademyAccountProfile.status)
        .where(AcademyAccountProfile.tenant_id == tenant_id)
        .where(AcademyAccountProfile.party_id == person.id)
    )
    if status != "active":
        raise BadRequestError("facilitator nominee must be an active user")
    return person


def _target_courses(db: Session, command: NominateFacilitatorCommand) -> tuple[UUID | None, UUID | None, list[UUID]]:
    if command.scope is FacilitatorScope.COURSE:
        if command.offering_id is None or command.cohort_id is not None:
            raise BadRequestError("course-scoped nomination requires only offering_id")
        offering = db.scalars(
            select(CourseOffering)
            .where(CourseOffering.tenant_id == command.tenant_id)
            .where(CourseOffering.id == command.offering_id)
        ).first()
        if offering is None:
            raise NotFoundError("course offering not found for tenant")
        if offering.status != "active":
            raise BadRequestError("course offering must be active")
        return None, offering.id, [offering.course_id]

    if command.cohort_id is None or command.offering_id is not None:
        raise BadRequestError("cohort-scoped nomination requires only cohort_id")
    cohort = db.scalars(
        select(Cohort).where(Cohort.tenant_id == command.tenant_id).where(Cohort.id == command.cohort_id)
    ).first()
    if cohort is None:
        raise NotFoundError("cohort not found for tenant")
    course_ids = list(
        db.scalars(
            select(CourseOffering.course_id)
            .where(CourseOffering.tenant_id == command.tenant_id)
            .where(CourseOffering.cohort_id == cohort.id)
            .where(CourseOffering.status == "active")
            .order_by(CourseOffering.course_id)
        ).all()
    )
    return cohort.id, None, course_ids


def _nomination_query(command: NominateFacilitatorCommand):
    query = (
        select(FacilitatorNomination)
        .where(FacilitatorNomination.tenant_id == command.tenant_id)
        .where(FacilitatorNomination.person_id == command.person_id)
        .where(FacilitatorNomination.scope_type == command.scope.value)
    )
    if command.scope is FacilitatorScope.COURSE:
        return query.where(FacilitatorNomination.offering_id == command.offering_id)
    return query.where(FacilitatorNomination.cohort_id == command.cohort_id)


def _insert_nomination(
    db: Session,
    *,
    command: NominateFacilitatorCommand,
    cohort_id: UUID | None,
    offering_id: UUID | None,
    now: datetime,
) -> UUID | None:
    values = {
        "id": uuid4(),
        "tenant_id": command.tenant_id,
        "person_id": command.person_id,
        "nominated_by_person_id": command.actor_person_id,
        "scope_type": command.scope.value,
        "cohort_id": cohort_id,
        "offering_id": offering_id,
        "status": FacilitatorStatus.PENDING.value,
        "created_at": now,
        "updated_at": now,
    }
    stmt = insert(FacilitatorNomination).values(**values)
    if command.scope is FacilitatorScope.COURSE:
        stmt = stmt.on_conflict_do_nothing(
            index_elements=[
                FacilitatorNomination.tenant_id,
                FacilitatorNomination.person_id,
                FacilitatorNomination.offering_id,
            ],
            index_where=text("scope_type = 'course'"),
        )
    else:
        stmt = stmt.on_conflict_do_nothing(
            index_elements=[
                FacilitatorNomination.tenant_id,
                FacilitatorNomination.person_id,
                FacilitatorNomination.cohort_id,
            ],
            index_where=text("scope_type = 'cohort'"),
        )
    return db.execute(stmt.returning(FacilitatorNomination.id)).scalar()


def _requirement_course_ids(db: Session, nomination: FacilitatorNomination) -> tuple[UUID, ...]:
    return tuple(
        db.scalars(
            select(FacilitatorRequirement.course_id)
            .where(FacilitatorRequirement.tenant_id == nomination.tenant_id)
            .where(FacilitatorRequirement.nomination_id == nomination.id)
            .order_by(FacilitatorRequirement.course_id)
        ).all()
    )


def _missing_course_ids(
    db: Session, *, tenant_id: UUID, person_id: UUID, required_course_ids: tuple[UUID, ...]
) -> tuple[UUID, ...]:
    if not required_course_ids:
        return ()
    completed = set(
        db.scalars(
            select(CourseCompletion.course_id)
            .where(CourseCompletion.tenant_id == tenant_id)
            .where(CourseCompletion.person_id == person_id)
            .where(CourseCompletion.course_id.in_(required_course_ids))
            .where(CourseCompletion.status == "completed")
        ).all()
    )
    return tuple(course_id for course_id in required_course_ids if course_id not in completed)


def _outcome(
    db: Session,
    nomination: FacilitatorNomination,
    *,
    created: bool,
    activated_now: bool,
) -> FacilitatorNominationOutcome:
    required = _requirement_course_ids(db, nomination)
    missing = _missing_course_ids(
        db,
        tenant_id=nomination.tenant_id,
        person_id=nomination.person_id,
        required_course_ids=required,
    )
    return FacilitatorNominationOutcome(
        nomination_id=nomination.id,
        scope=FacilitatorScope(nomination.scope_type),
        status=FacilitatorStatus(nomination.status),
        created=created,
        activated_now=activated_now,
        required_course_ids=required,
        missing_course_ids=missing,
    )


def _activate_eligible(
    db: Session,
    *,
    tenant_id: UUID,
    nomination_id: UUID | None = None,
    person_id: UUID | None = None,
    course_id: UUID | None = None,
    limit: int | None = None,
    actor_person_id: UUID | None = None,
) -> tuple[_ActivationAttempt, ...]:
    """Invoke the sole persistence capability for facilitator activation.

    The database capability owns eligibility revalidation, row locking, the
    status transition, and its fixed notification/audit consequences. Keeping
    this adapter typed prevents the restricted lab worker from requiring any
    direct privileges on facilitator or audit tables.
    """

    caller_bypasses_rls = bool(
        db.scalar(text("SELECT rolbypassrls FROM pg_catalog.pg_roles WHERE rolname = CURRENT_USER"))
    )
    if caller_bypasses_rls:
        # Offline/admin and cross-tenant worker roles deliberately bypass RLS,
        # so bind them to this typed command's tenant for the transaction. A
        # request role may never switch its own tenant here; it must already
        # carry the adapter-established tenant binding.
        db.execute(
            text("SELECT pg_catalog.set_config('app.current_tenant', :tenant_id, true)"),
            {"tenant_id": str(tenant_id)},
        )

    rows = db.execute(
        text(
            """SELECT candidate_nomination_id, activated_now
                 FROM public.activate_eligible_facilitator_nominations(
                     CAST(:tenant_id AS uuid), CAST(:nomination_id AS uuid),
                     CAST(:person_id AS uuid), CAST(:course_id AS uuid), :limit,
                     CAST(:actor_person_id AS uuid)
                 )"""
        ),
        {
            "tenant_id": tenant_id,
            "nomination_id": nomination_id,
            "person_id": person_id,
            "course_id": course_id,
            "limit": limit,
            "actor_person_id": actor_person_id,
        },
    ).all()
    return tuple(
        _ActivationAttempt(
            nomination_id=UUID(str(row.candidate_nomination_id)),
            activated_now=bool(row.activated_now),
        )
        for row in rows
    )


def nominate(db: Session, *, command: NominateFacilitatorCommand) -> FacilitatorNominationOutcome:
    """Create or idempotently return a nomination, activating it when eligible."""
    person = _require_active_nominee(db, tenant_id=command.tenant_id, person_id=command.person_id)
    _person_or_404(db, tenant_id=command.tenant_id, person_id=command.actor_person_id)
    cohort_id, offering_id, course_ids = _target_courses(db, command)
    now = datetime.now(UTC)
    inserted_id = _insert_nomination(
        db,
        command=command,
        cohort_id=cohort_id,
        offering_id=offering_id,
        now=now,
    )
    nomination = db.scalars(_nomination_query(command).with_for_update()).one()
    created = inserted_id is not None
    if not created and nomination.status != FacilitatorStatus.REVOKED.value:
        return _outcome(db, nomination, created=False, activated_now=False)

    if not created:
        nomination.status = FacilitatorStatus.PENDING.value
        nomination.nominated_by_person_id = command.actor_person_id
        nomination.activated_at = None
        nomination.revoked_at = None
        nomination.activation_reason = None
        db.execute(
            delete(FacilitatorRequirement)
            .where(FacilitatorRequirement.tenant_id == command.tenant_id)
            .where(FacilitatorRequirement.nomination_id == nomination.id)
        )
    for course_id in course_ids:
        db.add(
            FacilitatorRequirement(
                tenant_id=command.tenant_id,
                nomination_id=nomination.id,
                course_id=course_id,
            )
        )
    db.flush()
    notify(
        db,
        tenant_id=command.tenant_id,
        person_id=person.id,
        kind="facilitator_nomination",
        title="You have been nominated as a facilitator",
        body="Your facilitator assignment will activate after you complete its required course work.",
        link="/learn",
    )
    write_audit_event(
        db,
        tenant_id=command.tenant_id,
        actor_type="user",
        actor_id=str(command.actor_person_id),
        actor_party_id=command.actor_person_id,
        source_application="dotmac_academy_app",
        action="facilitator_nomination.create",
        entity_type="facilitator_nomination",
        entity_id=str(nomination.id),
        details={"scope": command.scope.value, "person_id": str(command.person_id)},
    )
    activation = _activate_eligible(
        db,
        tenant_id=command.tenant_id,
        nomination_id=nomination.id,
        actor_person_id=command.actor_person_id,
    )
    activated = bool(activation and activation[0].activated_now)
    if activated:
        db.refresh(nomination)
    return _outcome(db, nomination, created=created, activated_now=activated)


def reconcile(db: Session, *, command: ReconcileFacilitatorCommand) -> FacilitatorNominationOutcome:
    nomination = db.scalars(
        select(FacilitatorNomination)
        .where(FacilitatorNomination.tenant_id == command.tenant_id)
        .where(FacilitatorNomination.id == command.nomination_id)
        .with_for_update()
    ).first()
    if nomination is None:
        raise NotFoundError("facilitator nomination not found")
    activation = _activate_eligible(
        db,
        tenant_id=command.tenant_id,
        nomination_id=nomination.id,
    )
    activated = bool(activation and activation[0].activated_now)
    if activated:
        db.refresh(nomination)
    return _outcome(db, nomination, created=False, activated_now=activated)


def reconcile_completed_course(
    db: Session,
    *,
    command: ReconcileCompletedCourseCommand,
) -> FacilitatorSweepOutcome:
    """Activate nominations affected by one newly completed course.

    The completion writer calls this in the same transaction for immediate
    access. ``sweep_pending`` remains the idempotent repair path if that
    participant consequence ever needs to be replayed.
    """

    attempts = _activate_eligible(
        db,
        tenant_id=command.tenant_id,
        person_id=command.person_id,
        course_id=command.course_id,
    )
    activated = sum(attempt.activated_now for attempt in attempts)
    return FacilitatorSweepOutcome(
        examined=len(attempts),
        activated=activated,
        pending=len(attempts) - activated,
    )


def revoke(db: Session, *, command: RevokeFacilitatorCommand) -> FacilitatorNominationOutcome:
    _person_or_404(db, tenant_id=command.tenant_id, person_id=command.actor_person_id)
    nomination = db.scalars(
        select(FacilitatorNomination)
        .where(FacilitatorNomination.tenant_id == command.tenant_id)
        .where(FacilitatorNomination.id == command.nomination_id)
        .with_for_update()
    ).first()
    if nomination is None:
        raise NotFoundError("facilitator nomination not found")
    if nomination.status != FacilitatorStatus.REVOKED.value:
        nomination.status = FacilitatorStatus.REVOKED.value
        nomination.revoked_at = datetime.now(UTC)
        write_audit_event(
            db,
            tenant_id=command.tenant_id,
            actor_type="user",
            actor_id=str(command.actor_person_id),
            actor_party_id=command.actor_person_id,
            source_application="dotmac_academy_app",
            action="facilitator_nomination.revoke",
            entity_type="facilitator_nomination",
            entity_id=str(nomination.id),
            details={"scope": nomination.scope_type, "person_id": str(nomination.person_id)},
        )
        db.flush()
    return _outcome(db, nomination, created=False, activated_now=False)


def sweep_pending(db: Session, *, command: SweepFacilitatorsCommand) -> FacilitatorSweepOutcome:
    if command.limit < 1:
        raise BadRequestError("sweep limit must be positive")
    if command.limit > 1000:
        raise BadRequestError("sweep limit must not exceed 1000")
    attempts = _activate_eligible(
        db,
        tenant_id=command.tenant_id,
        limit=command.limit,
    )
    activated = sum(attempt.activated_now for attempt in attempts)
    return FacilitatorSweepOutcome(
        examined=len(attempts),
        activated=activated,
        pending=len(attempts) - activated,
    )
