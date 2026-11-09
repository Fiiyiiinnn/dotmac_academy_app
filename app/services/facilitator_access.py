"""Read-only authorization projection for legacy and scoped facilitators.

Tenant role grants remain authoritative for administrators and legacy
instructors. New facilitator authority comes only from an active
``FacilitatorNomination``; pending and revoked nominations are deliberately
absent from this projection.
"""

# Ported at new-composition genesis (identity/facilitator slice).

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from dotmac_learning.participation_models import Cohort, CourseOffering, Enrollment
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.facilitator import FacilitatorNomination, FacilitatorRequirement
from app.services.roles import role_slugs


@dataclass(frozen=True)
class FacilitatorAccess:
    """Typed authorization facts for one person in one tenant."""

    is_admin: bool
    is_legacy_instructor: bool
    cohort_ids: frozenset[UUID]
    offering_ids: frozenset[UUID]
    course_cohort_ids: frozenset[UUID]
    legacy_offering_ids: frozenset[UUID]
    cohort_offering_ids: frozenset[UUID]

    @property
    def has_global_staff_access(self) -> bool:
        return self.is_admin or self.is_legacy_instructor

    @property
    def has_broad_access(self) -> bool:
        """Compatibility name for whole-cohort and global page checks."""

        return self.has_global_staff_access

    @property
    def has_active_assignment(self) -> bool:
        return bool(self.cohort_ids or self.offering_ids)

    @property
    def can_teach(self) -> bool:
        return self.has_global_staff_access or self.has_active_assignment

    @property
    def visible_cohort_ids(self) -> frozenset[UUID]:
        return self.cohort_ids | self.course_cohort_ids

    def allows_cohort(self, cohort_id: UUID) -> bool:
        """Whether whole-cohort data is authorized.

        A course assignment intentionally does not satisfy this check: exposing
        a cohort gradebook or report would include courses outside its scope.
        """

        return self.has_global_staff_access or cohort_id in self.cohort_ids

    def allows_offering(self, offering_id: UUID) -> bool:
        return self.is_admin or offering_id in (self.offering_ids | self.cohort_offering_ids | self.legacy_offering_ids)


def facilitator_access(db: Session, *, tenant_id: UUID, person_id: UUID) -> FacilitatorAccess:
    """Load effective teaching access without mutating roles or nominations."""

    slugs = role_slugs(db, tenant_id, person_id)
    nominations = db.execute(
        select(
            FacilitatorNomination.id,
            FacilitatorNomination.scope_type,
            FacilitatorNomination.cohort_id,
            FacilitatorNomination.offering_id,
            CourseOffering.cohort_id,
        )
        .outerjoin(
            CourseOffering,
            (CourseOffering.tenant_id == FacilitatorNomination.tenant_id)
            & (CourseOffering.id == FacilitatorNomination.offering_id),
        )
        .where(FacilitatorNomination.tenant_id == tenant_id)
        .where(FacilitatorNomination.person_id == person_id)
        .where(FacilitatorNomination.status == "active")
        .where((FacilitatorNomination.scope_type == "cohort") | (CourseOffering.status == "active"))
    ).all()
    cohort_ids = frozenset(
        nomination_cohort_id
        for _, scope_type, nomination_cohort_id, _, _ in nominations
        if scope_type == "cohort" and nomination_cohort_id is not None
    )
    offering_ids = frozenset(
        offering_id
        for _, scope_type, _, offering_id, _ in nominations
        if scope_type == "course" and offering_id is not None
    )
    course_cohort_ids = frozenset(
        offering_cohort_id
        for _, scope_type, _, _, offering_cohort_id in nominations
        if scope_type == "course" and offering_cohort_id is not None
    )
    # Cohort authority is bounded by the requirement snapshot. A course added
    # to the cohort later must not silently widen an existing assignment.
    cohort_offering_ids = frozenset(
        db.scalars(
            select(CourseOffering.id)
            .join(
                FacilitatorRequirement,
                (FacilitatorRequirement.tenant_id == CourseOffering.tenant_id)
                & (FacilitatorRequirement.course_id == CourseOffering.course_id),
            )
            .join(
                FacilitatorNomination,
                (FacilitatorNomination.tenant_id == FacilitatorRequirement.tenant_id)
                & (FacilitatorNomination.id == FacilitatorRequirement.nomination_id),
            )
            .where(FacilitatorNomination.tenant_id == tenant_id)
            .where(FacilitatorNomination.person_id == person_id)
            .where(FacilitatorNomination.scope_type == "cohort")
            .where(FacilitatorNomination.status == "active")
            .where(CourseOffering.cohort_id == FacilitatorNomination.cohort_id)
            .where(CourseOffering.status == "active")
        ).all()
    )
    legacy_offering_ids = (
        frozenset(
            db.scalars(
                select(CourseOffering.id)
                .join(
                    Enrollment,
                    (Enrollment.tenant_id == CourseOffering.tenant_id)
                    & (Enrollment.cohort_id == CourseOffering.cohort_id),
                )
                .where(CourseOffering.tenant_id == tenant_id)
                .where(CourseOffering.status == "active")
                .where(Enrollment.person_id == person_id)
                .where(Enrollment.role_in_cohort == "instructor")
                .where(Enrollment.status == "active")
            ).all()
        )
        if "instructor" in slugs
        else frozenset()
    )
    return FacilitatorAccess(
        is_admin="admin" in slugs,
        is_legacy_instructor="instructor" in slugs,
        cohort_ids=cohort_ids,
        offering_ids=offering_ids,
        course_cohort_ids=course_cohort_ids,
        legacy_offering_ids=legacy_offering_ids,
        cohort_offering_ids=cohort_offering_ids,
    )


def authorized_cohort_ids(db: Session, *, tenant_id: UUID, access: FacilitatorAccess) -> frozenset[UUID]:
    """Return cohorts that may appear in the person's teaching workspace."""

    if not access.has_broad_access:
        return access.visible_cohort_ids
    return frozenset(db.scalars(select(Cohort.id).where(Cohort.tenant_id == tenant_id)).all())


def authorized_offering_ids(db: Session, *, tenant_id: UUID, access: FacilitatorAccess) -> frozenset[UUID]:
    """Return exact offerings the person may facilitate."""

    if access.is_admin:
        query = (
            select(CourseOffering.id)
            .where(CourseOffering.tenant_id == tenant_id)
            .where(CourseOffering.status == "active")
        )
    else:
        return access.offering_ids | access.cohort_offering_ids | access.legacy_offering_ids
    return frozenset(db.scalars(query).all())


def allows_person(
    db: Session,
    *,
    tenant_id: UUID,
    access: FacilitatorAccess,
    target_person_id: UUID,
) -> bool:
    """Whether the facilitator may read a person's whole learning transcript."""

    if access.has_broad_access:
        return True
    if not access.cohort_ids:
        return False
    return (
        db.scalar(
            select(Enrollment.id)
            .where(Enrollment.tenant_id == tenant_id)
            .where(Enrollment.person_id == target_person_id)
            .where(Enrollment.cohort_id.in_(access.cohort_ids))
            .where(Enrollment.status == "active")
            .limit(1)
        )
        is not None
    )
