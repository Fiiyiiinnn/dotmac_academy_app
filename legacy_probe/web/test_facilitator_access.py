"""Focused web authorization tests for scoped facilitator nominations."""

from __future__ import annotations

from dotmac_kernel.models import Party

from app.models.cohort import Cohort, Enrollment
from app.models.course import Course
from app.models.facilitator import FacilitatorNomination, FacilitatorRequirement
from app.models.offering import CourseOffering
from app.services.facilitator_access import facilitator_access
from app.services.roles import role_slugs
from app.web.instructor import _assessment_person_ids_for_course
from legacy_probe.conftest import make_canonical_account, web_login
from legacy_probe.web.test_reports import _seed_cohort


def _scoped_user(db, tenant, *, email: str) -> Party:
    # A scoped facilitator is an ordinary student account (student grant only,
    # so the web facet admits it) whose teaching authority comes afterwards
    # from an active nomination — never from a staff role grant.
    person = make_canonical_account(
        db,
        tenant_id=tenant.id,
        email=email,
        first_name="Scoped",
        last_name="Facilitator",
        password="password1",
        role="student",
    )
    db.flush()
    return person


def _login(client, email: str) -> dict[str, str]:
    headers = {"Host": "alpha.localhost"}
    web_login(client, headers, email, "password1")
    return headers


def test_pending_and_revoked_nominations_confer_no_teaching_access(app_client, admin_session, tenant_a):
    cohort, _ = _seed_cohort(admin_session, tenant_a.id)
    for status, email in (
        ("pending", "pending-facilitator@a.edu"),
        ("revoked", "revoked-facilitator@a.edu"),
    ):
        person = _scoped_user(admin_session, tenant_a, email=email)
        admin_session.add(
            FacilitatorNomination(
                tenant_id=tenant_a.id,
                person_id=person.id,
                scope_type="cohort",
                cohort_id=cohort.id,
                status=status,
            )
        )
        admin_session.commit()

        access = facilitator_access(admin_session, tenant_id=tenant_a.id, person_id=person.id)
        assert access.can_teach is False
        headers = _login(app_client, email)
        assert app_client.get("/instructor", headers=headers).status_code == 403


def test_active_cohort_nomination_is_scoped_without_person_role(app_client, admin_session, tenant_a):
    cohort, student = _seed_cohort(admin_session, tenant_a.id)
    other = Cohort(
        tenant_id=tenant_a.id,
        name="Unassigned cohort",
        discipline="management",
        status="active",
    )
    facilitator = _scoped_user(admin_session, tenant_a, email="cohort-facilitator@a.edu")
    admin_session.add(other)
    admin_session.flush()
    admin_session.add(
        FacilitatorNomination(
            tenant_id=tenant_a.id,
            person_id=facilitator.id,
            scope_type="cohort",
            cohort_id=cohort.id,
            status="active",
        )
    )
    admin_session.commit()

    # The nomination confers no staff grant: the account still holds only its
    # ordinary student role (composed equivalent of MAIN's zero-PersonRole
    # assertion, where MAIN accounts needed no grant to authenticate).
    assert role_slugs(admin_session, tenant_a.id, facilitator.id) == {"student"}
    headers = _login(app_client, "cohort-facilitator@a.edu")
    home = app_client.get("/instructor", headers=headers)
    assert home.status_code == 200
    assert cohort.name in home.text
    assert other.name not in home.text
    assert app_client.get(f"/instructor/gradebook/{cohort.id}", headers=headers).status_code == 200
    assert app_client.get(f"/instructor/gradebook/{other.id}", headers=headers).status_code == 403
    assert app_client.get(f"/instructor/reports/student/{student.id}", headers=headers).status_code == 200
    assert app_client.get("/admin/users", headers=headers).status_code == 403
    assert app_client.get("/success-queue", headers=headers).status_code == 403
    assert app_client.get("/instructor/labs", headers=headers).status_code == 403
    assert app_client.get("/instructor/reports/academy-summary.csv", headers=headers).status_code == 403


def test_active_course_nomination_cannot_read_whole_cohort_data(app_client, admin_session, tenant_a):
    cohort, student = _seed_cohort(admin_session, tenant_a.id)
    offering = admin_session.query(CourseOffering).filter_by(tenant_id=tenant_a.id, cohort_id=cohort.id).one()
    facilitator = _scoped_user(admin_session, tenant_a, email="course-facilitator@a.edu")
    admin_session.add(
        FacilitatorNomination(
            tenant_id=tenant_a.id,
            person_id=facilitator.id,
            scope_type="course",
            offering_id=offering.id,
            status="active",
        )
    )
    admin_session.commit()

    access = facilitator_access(admin_session, tenant_id=tenant_a.id, person_id=facilitator.id)
    assert access.allows_offering(offering.id) is True
    assert access.allows_cohort(cohort.id) is False
    headers = _login(app_client, "course-facilitator@a.edu")
    assert app_client.get("/instructor", headers=headers).status_code == 200
    assert app_client.get("/instructor/gradebook", headers=headers).status_code == 403
    assert app_client.get(f"/instructor/gradebook/{cohort.id}", headers=headers).status_code == 403
    assert app_client.get("/instructor/reports", headers=headers).status_code == 403
    assert app_client.get(f"/instructor/reports/student/{student.id}", headers=headers).status_code == 403


def test_cross_tenant_target_is_404_before_scope_check(app_client, admin_session, tenant_a, tenant_b):
    cohort, _ = _seed_cohort(admin_session, tenant_a.id)
    other = Cohort(
        tenant_id=tenant_b.id,
        name="Other tenant cohort",
        discipline="networking",
        status="active",
    )
    facilitator = _scoped_user(admin_session, tenant_a, email="tenant-facilitator@a.edu")
    admin_session.add(other)
    admin_session.flush()
    admin_session.add(
        FacilitatorNomination(
            tenant_id=tenant_a.id,
            person_id=facilitator.id,
            scope_type="cohort",
            cohort_id=cohort.id,
            status="active",
        )
    )
    admin_session.commit()

    headers = _login(app_client, "tenant-facilitator@a.edu")
    assert app_client.get(f"/instructor/gradebook/{other.id}", headers=headers).status_code == 404
    assert app_client.get(f"/instructor/reports/cohort/{other.id}", headers=headers).status_code == 404


def test_cohort_assessment_scope_excludes_ambiguous_shared_course_learners(admin_session, tenant_a):
    assigned = Cohort(tenant_id=tenant_a.id, name="Assigned", discipline="networking", status="active")
    other = Cohort(tenant_id=tenant_a.id, name="Other", discipline="networking", status="active")
    course = Course(
        tenant_id=tenant_a.id,
        slug="shared-assessment-course",
        title="Shared Assessment Course",
        discipline="networking",
        source_ref="test",
        version=1,
        status="published",
        listed=False,
    )
    facilitator = _scoped_user(admin_session, tenant_a, email="shared-course-facilitator@a.edu")
    assigned_only = _scoped_user(admin_session, tenant_a, email="assigned-only@a.edu")
    ambiguous = _scoped_user(admin_session, tenant_a, email="ambiguous@a.edu")
    admin_session.add_all([assigned, other, course])
    admin_session.flush()
    assigned_offering = CourseOffering(
        tenant_id=tenant_a.id,
        cohort_id=assigned.id,
        course_id=course.id,
        status="active",
    )
    other_offering = CourseOffering(
        tenant_id=tenant_a.id,
        cohort_id=other.id,
        course_id=course.id,
        status="active",
    )
    nomination = FacilitatorNomination(
        tenant_id=tenant_a.id,
        person_id=facilitator.id,
        scope_type="cohort",
        cohort_id=assigned.id,
        status="active",
    )
    admin_session.add_all([assigned_offering, other_offering, nomination])
    admin_session.flush()
    admin_session.add(
        FacilitatorRequirement(
            tenant_id=tenant_a.id,
            nomination_id=nomination.id,
            course_id=course.id,
        )
    )
    admin_session.add_all(
        [
            Enrollment(
                tenant_id=tenant_a.id,
                cohort_id=assigned.id,
                person_id=assigned_only.id,
                role_in_cohort="student",
                status="active",
            ),
            Enrollment(
                tenant_id=tenant_a.id,
                cohort_id=assigned.id,
                person_id=ambiguous.id,
                role_in_cohort="student",
                status="active",
            ),
            Enrollment(
                tenant_id=tenant_a.id,
                cohort_id=other.id,
                person_id=ambiguous.id,
                role_in_cohort="student",
                status="active",
            ),
        ]
    )
    admin_session.flush()

    people = _assessment_person_ids_for_course(
        admin_session,
        tenant_id=tenant_a.id,
        instructor_id=facilitator.id,
        course_id=course.id,
    )

    assert people == frozenset({assigned_only.id})
