"""Dedicated instructor cohort workspace web coverage."""

from __future__ import annotations

from dotmac_kernel.models import Party
from sqlalchemy import select

from app.models.assessment import Activity
from app.models.cohort import Cohort, Enrollment
from app.models.completion import CourseCompletion
from app.models.course import Chapter, Course
from app.models.facilitator import FacilitatorNomination
from app.models.offering import CourseOffering
from legacy_probe.conftest import make_canonical_account
from legacy_probe.web.test_reports import _login, _seed_login


def _seed_detail(admin_session, tenant):
    cohort = Cohort(
        tenant_id=tenant.id,
        name="Fiber Academy 2027",
        discipline="networking",
        status="active",
        delivery_mode="blended",
    )
    course = Course(
        tenant_id=tenant.id,
        slug="fiber-safety",
        title="Fiber Safety",
        discipline="networking",
        source_ref="test",
        version=2,
        status="published",
        listed=False,
    )
    admin_session.add_all([cohort, course])
    admin_session.flush()
    learner = make_canonical_account(
        admin_session,
        tenant_id=tenant.id,
        email="learner@a.edu",
        first_name="Ada",
        last_name="Learner",
        password="password1",
        role="student",
    )
    facilitator = make_canonical_account(
        admin_session,
        tenant_id=tenant.id,
        email="facilitator@a.edu",
        first_name="Femi",
        last_name="Facilitator",
        password="password1",
        role="instructor",
    )
    admin_session.add_all(
        [
            CourseOffering(
                tenant_id=tenant.id,
                cohort_id=cohort.id,
                course_id=course.id,
                status="active",
            ),
            Chapter(
                tenant_id=tenant.id,
                course_id=course.id,
                number=1,
                title="Module One",
                part="Foundations",
                body_html="<p>Safety first.</p>",
                order_index=1,
            ),
            Activity(
                tenant_id=tenant.id,
                course_id=course.id,
                chapter_number=1,
                type="mcq_test",
                title="Safety Check",
                pass_threshold=0.7, grading="auto", weight=1.0,
            ),
            Enrollment(
                tenant_id=tenant.id,
                cohort_id=cohort.id,
                person_id=learner.id,
                role_in_cohort="student",
                status="active",
            ),
            Enrollment(
                tenant_id=tenant.id,
                cohort_id=cohort.id,
                person_id=facilitator.id,
                role_in_cohort="instructor",
                status="active",
            ),
            FacilitatorNomination(
                tenant_id=tenant.id,
                person_id=facilitator.id,
                scope_type="cohort",
                cohort_id=cohort.id,
                status="active",
                activation_reason="legacy_grandfathered",
            ),
            CourseCompletion(
                tenant_id=tenant.id,
                person_id=learner.id,
                course_id=course.id,
                status="in_progress",
                pct=0.6,
            ),
        ]
    )
    admin_session.commit()
    return cohort


def test_admin_sees_complete_cohort_workspace(app_client, admin_session, tenant_a):
    _seed_login(admin_session, tenant_a, "admin@a.edu", "admin")
    cohort = _seed_detail(admin_session, tenant_a)
    response = app_client.get(
        f"/instructor/cohorts/{cohort.id}",
        headers=_login(app_client, "admin@a.edu"),
    )

    assert response.status_code == 200
    assert "Fiber Academy 2027" in response.text
    assert "learner@a.edu" in response.text
    assert "60%" in response.text
    assert "Fiber Safety" in response.text
    assert "Module One" in response.text
    assert "Safety Check" in response.text
    assert "facilitator@a.edu" in response.text
    assert f'/instructor/gradebook/{cohort.id}' in response.text
    assert f'/instructor/reports/cohort/{cohort.id}' in response.text
    assert f'/instructor/cohorts/{cohort.id}/invite' in response.text
    assert f'/instructor/cohorts/{cohort.id}/enroll' in response.text
    assert f'/instructor/cohorts/{cohort.id}/roster/' in response.text
    assert 'name="return_to"' in response.text
    assert 'Un-enroll' in response.text
    assert 'Send invite and enroll' in response.text
    assert 'Enroll students' in response.text
    course = admin_session.scalars(
        select(Course)
        .where(Course.tenant_id == tenant_a.id)
        .where(Course.slug == "fiber-safety")
    ).one()
    assert f'/instructor/courses/{course.id}/preview' in response.text
    assert f'/instructor/courses/{course.id}/edit' in response.text
    assert 'Open course content' in response.text
    assert 'Edit course' in response.text


def test_admin_can_unenroll_student_from_cohort_workspace(app_client, admin_session, tenant_a):
    _seed_login(admin_session, tenant_a, "admin@a.edu", "admin")
    cohort = _seed_detail(admin_session, tenant_a)
    learner = admin_session.scalars(
        select(Party).where(Party.tenant_id == tenant_a.id).where(Party.email == "learner@a.edu")
    ).one()
    headers = _login(app_client, "admin@a.edu")
    csrf = app_client.cookies.get("csrf_token", "")
    workspace = f"/instructor/cohorts/{cohort.id}"

    response = app_client.post(
        f"{workspace}/roster/{learner.id}/state",
        headers={**headers, "x-csrf-token": csrf, "HX-Request": "true"},
        data={"state": "dropped", "return_to": workspace},
    )

    assert response.status_code == 200
    assert response.headers.get("HX-Redirect") == workspace
    admin_session.expire_all()
    enrollment = admin_session.scalars(
        select(Enrollment)
        .where(Enrollment.cohort_id == cohort.id)
        .where(Enrollment.person_id == learner.id)
    ).one()
    assert enrollment.status == "dropped"


def test_admin_can_enroll_existing_student_from_cohort_workspace(app_client, admin_session, tenant_a):
    _seed_login(admin_session, tenant_a, "admin@a.edu", "admin")
    cohort = _seed_detail(admin_session, tenant_a)
    learner = make_canonical_account(
        admin_session,
        tenant_id=tenant_a.id,
        email="existing@a.edu",
        first_name="Existing",
        last_name="Learner",
        password="password1",
        role="student",
    )
    admin_session.commit()
    headers = _login(app_client, "admin@a.edu")
    csrf = app_client.cookies.get("csrf_token", "")
    workspace = f"/instructor/cohorts/{cohort.id}"

    response = app_client.post(
        f"{workspace}/enroll",
        headers={**headers, "x-csrf-token": csrf, "HX-Request": "true"},
        data={"emails": learner.email, "return_to": workspace},
    )

    assert response.status_code == 200
    assert response.headers.get("HX-Redirect") == workspace
    admin_session.expire_all()
    enrollment = admin_session.scalars(
        select(Enrollment)
        .where(Enrollment.cohort_id == cohort.id)
        .where(Enrollment.person_id == learner.id)
    ).one()
    assert enrollment.status == "active"
    assert enrollment.role_in_cohort == "student"


def test_admin_can_invite_student_from_cohort_workspace(app_client, admin_session, tenant_a):
    _seed_login(admin_session, tenant_a, "admin@a.edu", "admin")
    cohort = _seed_detail(admin_session, tenant_a)
    headers = _login(app_client, "admin@a.edu")
    csrf = app_client.cookies.get("csrf_token", "")
    workspace = f"/instructor/cohorts/{cohort.id}"

    response = app_client.post(
        f"{workspace}/invite",
        headers={**headers, "x-csrf-token": csrf, "HX-Request": "true"},
        data={
            "email": "invited@a.edu",
            "first_name": "Invited",
            "last_name": "Learner",
            "return_to": workspace,
        },
    )

    assert response.status_code == 200
    assert response.headers.get("HX-Redirect") == workspace
    admin_session.expire_all()
    learner = admin_session.scalars(
        select(Party).where(Party.tenant_id == tenant_a.id).where(Party.email == "invited@a.edu")
    ).one()
    enrollment = admin_session.scalars(
        select(Enrollment)
        .where(Enrollment.cohort_id == cohort.id)
        .where(Enrollment.person_id == learner.id)
    ).one()
    assert enrollment.status == "active"


def test_cohort_action_rejects_external_return_target(app_client, admin_session, tenant_a):
    _seed_login(admin_session, tenant_a, "admin@a.edu", "admin")
    cohort = _seed_detail(admin_session, tenant_a)
    learner = admin_session.scalars(
        select(Party).where(Party.tenant_id == tenant_a.id).where(Party.email == "learner@a.edu")
    ).one()
    headers = _login(app_client, "admin@a.edu")
    csrf = app_client.cookies.get("csrf_token", "")

    response = app_client.post(
        f"/instructor/cohorts/{cohort.id}/roster/{learner.id}/state",
        headers={**headers, "x-csrf-token": csrf, "HX-Request": "true"},
        data={"state": "dropped", "return_to": "https://evil.example"},
    )

    assert response.status_code == 200
    assert response.headers.get("HX-Redirect") == "/instructor/cohorts"


def test_cohort_workspace_student_table_is_paginated(app_client, admin_session, tenant_a):
    _seed_login(admin_session, tenant_a, "admin@a.edu", "admin")
    cohort = Cohort(tenant_id=tenant_a.id, name="Large Cohort", discipline="fiber", status="active")
    admin_session.add(cohort)
    admin_session.flush()
    for index in range(13):
        learner = make_canonical_account(
            admin_session,
            tenant_id=tenant_a.id,
            email=f"learner{index:02d}@a.edu",
            first_name=f"Learner {index:02d}",
            last_name="Student",
            password="password1",
            role="student",
        )
        admin_session.add(
            Enrollment(
                tenant_id=tenant_a.id,
                cohort_id=cohort.id,
                person_id=learner.id,
                role_in_cohort="student",
                status="active",
            )
        )
    admin_session.commit()

    response = app_client.get(
        f"/instructor/cohorts/{cohort.id}?limit=6&offset=0",
        headers=_login(app_client, "admin@a.edu"),
    )

    assert response.status_code == 200
    assert "Showing <strong>1</strong> to <strong>6</strong> of <strong>13</strong> students" in response.text
    assert "learner00@a.edu" in response.text
    assert "learner05@a.edu" in response.text
    assert "learner06@a.edu" not in response.text


def test_cohort_workspace_is_tenant_scoped(app_client, admin_session, tenant_a, tenant_b):
    _seed_login(admin_session, tenant_a, "admin@a.edu", "admin")
    other = Cohort(tenant_id=tenant_b.id, name="Other Tenant", discipline="fiber", status="active")
    admin_session.add(other)
    admin_session.commit()

    response = app_client.get(
        f"/instructor/cohorts/{other.id}",
        headers=_login(app_client, "admin@a.edu"),
    )

    assert response.status_code == 404
