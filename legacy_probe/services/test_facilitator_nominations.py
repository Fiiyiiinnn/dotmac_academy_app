"""Facilitator nominations preserve learner state and activate from completion facts."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from dotmac_kernel.audit_actions import AuditActionRegistry
from dotmac_kernel.models import Party, PartyPerson, PartyRoleGrant, PartyType
from sqlalchemy import select, text

from app.models.cohort import Cohort, Enrollment
from app.models.completion import CourseCompletion
from app.models.course import Course
from app.models.facilitator import FacilitatorNomination, FacilitatorRequirement
from app.models.identity_extensions import AcademyAccountProfile
from app.models.notification import Notification
from app.models.offering import CourseOffering
from app.models.rbac import AuditEvent
from app.models.track import CohortTrack, Track
from app.owner_composition import compose_migration_owner_declarations
from app.services.facilitator_nominations import (
    FacilitatorScope,
    FacilitatorStatus,
    NominateFacilitatorCommand,
    RevokeFacilitatorCommand,
    SweepFacilitatorsCommand,
    nominate,
    revoke,
    sweep_pending,
)


@pytest.fixture(autouse=True)
def _audit_registry_installed(monkeypatch):
    # The registry is process-global and installed only by the app entry points
    # (create_app / bind_academy_database_runtime); install the composed one.
    registry = AuditActionRegistry.from_manifests(compose_migration_owner_declarations())
    monkeypatch.setattr("dotmac_kernel.audit_actions._active_registry", registry)


def _person(db, tenant_id, email):
    # Canonical person account components (Party + PartyPerson + active
    # profile) with no credential and no role grant: the nomination service
    # must not confer one, which the role-grant assertions below pin.
    person = Party(
        tenant_id=tenant_id,
        party_type=PartyType.person,
        display_name="Fac Candidate",
        email=email,
        is_active=True,
    )
    db.add(person)
    db.flush()
    db.add(PartyPerson(party_id=person.id, first_name="Fac", last_name="Candidate"))
    db.flush()
    now = datetime.now(UTC)
    db.add(
        AcademyAccountProfile(
            tenant_id=tenant_id, party_id=person.id, status="active", prefs={}, created_at=now, updated_at=now
        )
    )
    db.flush()
    return person


def _cohort(db, tenant_id, name):
    cohort = Cohort(tenant_id=tenant_id, name=name, discipline="networking", status="active")
    db.add(cohort)
    db.flush()
    return cohort


def _course(db, tenant_id, slug):
    course = Course(
        tenant_id=tenant_id,
        slug=slug,
        title=slug.replace("-", " ").title(),
        discipline="networking",
        source_ref="test",
        version=1,
        status="published",
        listed=False,
    )
    db.add(course)
    db.flush()
    return course


def _offering(db, tenant_id, cohort_id, course_id):
    offering = CourseOffering(
        tenant_id=tenant_id,
        cohort_id=cohort_id,
        course_id=course_id,
        status="active",
    )
    db.add(offering)
    db.flush()
    return offering


def test_incomplete_active_learner_nomination_stays_pending_without_mutating_learning_state(admin_session, tenant_a):
    actor = _person(admin_session, tenant_a.id, "admin-nominator@a.edu")
    learner = _person(admin_session, tenant_a.id, "learner-nominee@a.edu")
    cohort = _cohort(admin_session, tenant_a.id, "Pending Cohort")
    course = _course(admin_session, tenant_a.id, "pending-course")
    _offering(admin_session, tenant_a.id, cohort.id, course.id)
    track = Track(tenant_id=tenant_a.id, slug="pending-track", name="Pending Track")
    admin_session.add(track)
    admin_session.flush()
    admin_session.add(
        CohortTrack(
            tenant_id=tenant_a.id,
            cohort_id=cohort.id,
            track_id=track.id,
            status="active",
        )
    )
    admin_session.flush()
    admin_session.execute(
        text(
            """CREATE FUNCTION pg_temp.activate_eligible_facilitator_nominations(
                   uuid, uuid, uuid, uuid, integer, uuid
               ) RETURNS TABLE (candidate_nomination_id uuid, activated_now boolean)
               LANGUAGE plpgsql AS $$
               BEGIN
                   RAISE EXCEPTION 'unqualified activation capability call';
               END;
               $$"""
        )
    )
    enrollment = Enrollment(
        tenant_id=tenant_a.id,
        cohort_id=cohort.id,
        person_id=learner.id,
        track_id=track.id,
        role_in_cohort="student",
        status="active",
    )
    admin_session.add(enrollment)
    admin_session.flush()

    outcome = nominate(
        admin_session,
        command=NominateFacilitatorCommand(
            tenant_id=tenant_a.id,
            person_id=learner.id,
            actor_person_id=actor.id,
            scope=FacilitatorScope.COHORT,
            cohort_id=cohort.id,
        ),
    )

    assert outcome.status is FacilitatorStatus.PENDING
    assert outcome.required_course_ids == (course.id,)
    assert outcome.missing_course_ids == (course.id,)
    assert enrollment.role_in_cohort == "student"
    assert enrollment.status == "active"
    assert enrollment.track_id == track.id
    assert admin_session.query(PartyRoleGrant).filter_by(tenant_id=tenant_a.id, party_id=learner.id).count() == 0
    notices = admin_session.scalars(
        select(Notification).where(Notification.tenant_id == tenant_a.id).where(Notification.person_id == learner.id)
    ).all()
    assert [notice.kind for notice in notices] == ["facilitator_nomination"]


def test_course_nomination_activates_immediately_when_requirement_is_completed(admin_session, tenant_a):
    actor = _person(admin_session, tenant_a.id, "course-actor@a.edu")
    nominee = _person(admin_session, tenant_a.id, "course-nominee@a.edu")
    cohort = _cohort(admin_session, tenant_a.id, "Course Scope")
    course = _course(admin_session, tenant_a.id, "course-scope")
    offering = _offering(admin_session, tenant_a.id, cohort.id, course.id)
    admin_session.add(
        CourseCompletion(
            tenant_id=tenant_a.id,
            person_id=nominee.id,
            course_id=course.id,
            status="completed",
            pct=1.0,
        )
    )
    admin_session.flush()

    outcome = nominate(
        admin_session,
        command=NominateFacilitatorCommand(
            tenant_id=tenant_a.id,
            person_id=nominee.id,
            actor_person_id=actor.id,
            scope=FacilitatorScope.COURSE,
            offering_id=offering.id,
        ),
    )

    assert outcome.status is FacilitatorStatus.ACTIVE
    assert outcome.activated_now is True
    assert outcome.required_course_ids == (course.id,)
    assert outcome.missing_course_ids == ()
    assert admin_session.query(PartyRoleGrant).filter_by(tenant_id=tenant_a.id, party_id=nominee.id).count() == 0
    notices = admin_session.scalars(
        select(Notification).where(Notification.tenant_id == tenant_a.id).where(Notification.person_id == nominee.id)
    ).all()
    assert [notice.kind for notice in notices] == ["facilitator_nomination", "facilitator_activated"]
    activation_audit = (
        admin_session.query(AuditEvent)
        .filter_by(
            tenant_id=tenant_a.id,
            action="facilitator_nomination.activate",
            entity_id=str(outcome.nomination_id),
        )
        .one()
    )
    assert (
        activation_audit.actor_type, activation_audit.actor_id,
        activation_audit.actor_party_id, activation_audit.source_application,
    ) == ("user", str(actor.id), actor.id, "dotmac_academy_app")


def test_cohort_nomination_snapshots_requirements_and_sweep_activates_once(admin_session, tenant_a):
    actor = _person(admin_session, tenant_a.id, "sweep-actor@a.edu")
    nominee = _person(admin_session, tenant_a.id, "sweep-nominee@a.edu")
    cohort = _cohort(admin_session, tenant_a.id, "Sweep Cohort")
    first = _course(admin_session, tenant_a.id, "sweep-first")
    second = _course(admin_session, tenant_a.id, "sweep-second")
    _offering(admin_session, tenant_a.id, cohort.id, first.id)
    _offering(admin_session, tenant_a.id, cohort.id, second.id)
    admin_session.add(
        CourseCompletion(
            tenant_id=tenant_a.id,
            person_id=nominee.id,
            course_id=first.id,
            status="completed",
            pct=1.0,
        )
    )
    admin_session.flush()

    first_outcome = nominate(
        admin_session,
        command=NominateFacilitatorCommand(
            tenant_id=tenant_a.id,
            person_id=nominee.id,
            actor_person_id=actor.id,
            scope=FacilitatorScope.COHORT,
            cohort_id=cohort.id,
        ),
    )
    duplicate = nominate(
        admin_session,
        command=NominateFacilitatorCommand(
            tenant_id=tenant_a.id,
            person_id=nominee.id,
            actor_person_id=actor.id,
            scope=FacilitatorScope.COHORT,
            cohort_id=cohort.id,
        ),
    )

    assert first_outcome.status is FacilitatorStatus.PENDING
    assert duplicate.nomination_id == first_outcome.nomination_id
    assert duplicate.created is False
    assert admin_session.query(FacilitatorNomination).count() == 1
    assert admin_session.query(FacilitatorRequirement).count() == 2
    assert admin_session.query(Notification).filter_by(person_id=nominee.id).count() == 1

    admin_session.add(
        CourseCompletion(
            tenant_id=tenant_a.id,
            person_id=nominee.id,
            course_id=second.id,
            status="completed",
            pct=1.0,
        )
    )
    admin_session.flush()
    swept = sweep_pending(
        admin_session,
        command=SweepFacilitatorsCommand(tenant_id=tenant_a.id),
    )
    swept_again = sweep_pending(
        admin_session,
        command=SweepFacilitatorsCommand(tenant_id=tenant_a.id),
    )

    assert swept.activated == 1
    assert swept_again.examined == 0
    nomination = admin_session.get(FacilitatorNomination, first_outcome.nomination_id)
    assert nomination is not None and nomination.status == "active"
    assert admin_session.query(Notification).filter_by(person_id=nominee.id).count() == 2


def test_empty_cohort_nomination_fails_closed_as_pending(admin_session, tenant_a):
    actor = _person(admin_session, tenant_a.id, "empty-actor@a.edu")
    nominee = _person(admin_session, tenant_a.id, "empty-nominee@a.edu")
    cohort = _cohort(admin_session, tenant_a.id, "Empty Cohort")

    outcome = nominate(
        admin_session,
        command=NominateFacilitatorCommand(
            tenant_id=tenant_a.id,
            person_id=nominee.id,
            actor_person_id=actor.id,
            scope=FacilitatorScope.COHORT,
            cohort_id=cohort.id,
        ),
    )

    assert outcome.status is FacilitatorStatus.PENDING
    assert outcome.required_course_ids == ()
    assert outcome.missing_course_ids == ()
    assert outcome.activated_now is False


def test_sweep_limit_is_applied_after_eligibility_filter(admin_session, tenant_a):
    actor = _person(admin_session, tenant_a.id, "bounded-sweep-actor@a.edu")
    waiting = _person(admin_session, tenant_a.id, "bounded-sweep-waiting@a.edu")
    eligible = _person(admin_session, tenant_a.id, "bounded-sweep-eligible@a.edu")
    cohort = _cohort(admin_session, tenant_a.id, "Bounded Sweep")
    course = _course(admin_session, tenant_a.id, "bounded-sweep-course")
    offering = _offering(admin_session, tenant_a.id, cohort.id, course.id)

    for nominee in (waiting, eligible):
        nominate(
            admin_session,
            command=NominateFacilitatorCommand(
                tenant_id=tenant_a.id,
                person_id=nominee.id,
                actor_person_id=actor.id,
                scope=FacilitatorScope.COURSE,
                offering_id=offering.id,
            ),
        )
    admin_session.add(
        CourseCompletion(
            tenant_id=tenant_a.id,
            person_id=eligible.id,
            course_id=course.id,
            status="completed",
            pct=1.0,
        )
    )
    admin_session.flush()

    swept = sweep_pending(
        admin_session,
        command=SweepFacilitatorsCommand(tenant_id=tenant_a.id, limit=1),
    )

    assert swept.examined == 1
    assert swept.activated == 1
    statuses = dict(admin_session.execute(select(FacilitatorNomination.person_id, FacilitatorNomination.status)).all())
    assert statuses[waiting.id] == "pending"
    assert statuses[eligible.id] == "active"


def test_revocation_is_idempotent_and_does_not_touch_enrollment(admin_session, tenant_a):
    actor = _person(admin_session, tenant_a.id, "revoke-actor@a.edu")
    nominee = _person(admin_session, tenant_a.id, "revoke-nominee@a.edu")
    cohort = _cohort(admin_session, tenant_a.id, "Revoke Cohort")
    course = _course(admin_session, tenant_a.id, "revoke-course")
    offering = _offering(admin_session, tenant_a.id, cohort.id, course.id)
    admin_session.add(
        CourseCompletion(
            tenant_id=tenant_a.id,
            person_id=nominee.id,
            course_id=course.id,
            status="completed",
            pct=1.0,
        )
    )
    enrollment = Enrollment(
        tenant_id=tenant_a.id,
        cohort_id=cohort.id,
        person_id=nominee.id,
        role_in_cohort="student",
        status="active",
    )
    admin_session.add(enrollment)
    admin_session.flush()
    nominated = nominate(
        admin_session,
        command=NominateFacilitatorCommand(
            tenant_id=tenant_a.id,
            person_id=nominee.id,
            actor_person_id=actor.id,
            scope=FacilitatorScope.COURSE,
            offering_id=offering.id,
        ),
    )
    command = RevokeFacilitatorCommand(
        tenant_id=tenant_a.id,
        nomination_id=nominated.nomination_id,
        actor_person_id=actor.id,
    )

    revoked = revoke(admin_session, command=command)
    revoked_again = revoke(admin_session, command=command)
    renominated = nominate(
        admin_session,
        command=NominateFacilitatorCommand(
            tenant_id=tenant_a.id,
            person_id=nominee.id,
            actor_person_id=actor.id,
            scope=FacilitatorScope.COURSE,
            offering_id=offering.id,
        ),
    )

    assert revoked.status is FacilitatorStatus.REVOKED
    assert revoked_again.status is FacilitatorStatus.REVOKED
    assert renominated.created is False
    assert renominated.status is FacilitatorStatus.ACTIVE
    assert enrollment.role_in_cohort == "student"
    assert enrollment.status == "active"
