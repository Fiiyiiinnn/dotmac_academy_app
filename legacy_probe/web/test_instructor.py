"""Tests for the instructor portal — Task 12 (cohorts, enroll, results, override)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from dotmac_kernel.models import Party, PartyPerson, PartyRoleGrant, PartyType, Role, UserCredential
from sqlalchemy import select

from app.models.cohort import Cohort, Enrollment
from app.models.completion import CourseCompletion
from app.models.course import Chapter, Course
from app.models.facilitator import FacilitatorNomination
from app.models.identity_extensions import AcademyAccountProfile, AcademyCredentialPolicy
from app.models.offering import CourseOffering
from app.services.security import hash_password
from app.services.tracks import create_cohort_track
from legacy_probe.conftest import web_login
from legacy_probe.services.roster_request_fixtures import roster_actors

assert roster_actors


def _canonical_roles(db, tenant_id):
    roles = {row.slug: row for row in db.scalars(select(Role).where(Role.tenant_id == tenant_id)).all()}
    for slug in ("admin", "instructor", "student"):
        if slug not in roles:
            roles[slug] = Role(tenant_id=tenant_id, slug=slug, name=slug)
            db.add(roles[slug])
    db.flush()
    return roles


def _canonical_person(db, *, tenant_id, email, first_name, last_name, status="active"):
    person = Party(
        tenant_id=tenant_id,
        party_type=PartyType.person,
        email=email,
        display_name=f"{first_name} {last_name}",
        is_active=True,
    )
    db.add(person)
    db.flush()
    db.add(PartyPerson(party_id=person.id, first_name=first_name, last_name=last_name))
    db.flush()
    db.add(
        AcademyAccountProfile(
            tenant_id=tenant_id,
            party_id=person.id,
            status=status,
            prefs={},
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    db.flush()
    return person


def _canonical_credential(db, *, tenant_id, party_id, email, password_hash):
    credential = UserCredential(tenant_id=tenant_id, party_id=party_id, password_hash=password_hash)
    db.add(credential)
    db.flush()
    db.add(
        AcademyCredentialPolicy(
            tenant_id=tenant_id,
            credential_id=credential.id,
            email=email,
            failed_login_attempts=0,
            lifecycle_version=0,
            reset_required=False,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    db.flush()
    return credential


def _login_instructor(app_client, admin_session, tenant):
    """Seed an instructor person, log in via TestClient, return Host header dict."""
    roles = _canonical_roles(admin_session, tenant.id)
    p = _canonical_person(admin_session, tenant_id=tenant.id, email="i@a.edu", first_name="In", last_name="Str")
    admin_session.add(p)
    admin_session.flush()
    admin_session.add(
        _canonical_credential(
            admin_session,
            tenant_id=tenant.id,
            party_id=p.id,
            email="i@a.edu",
            password_hash=hash_password("password1"),
        )
    )
    admin_session.add(PartyRoleGrant(tenant_id=tenant.id, party_id=p.id, role_id=roles["instructor"].id))
    admin_session.commit()

    h = {"Host": "alpha.localhost"}
    # Log in via the CSRF double-submit flow (web_login).
    # The 303 redirect to "/" is followed (default follow_redirects=True), which triggers
    # a GET "/" that makes the CSRF middleware set the csrf_token cookie in the jar.
    web_login(app_client, h, "i@a.edu", "password1")
    return h


def _grant_admin(admin_session, tenant):
    """Give the logged-in instructor (i@a.edu) the admin role too.

    Course creation is admin-only and authoring/publishing any course requires
    admin (or a per-course instructor assignment), so authoring tests act as admin.
    """
    from sqlalchemy import select

    roles = _canonical_roles(admin_session, tenant.id)
    person = admin_session.scalars(
        select(Party).where(Party.tenant_id == tenant.id).where(Party.email == "i@a.edu")
    ).first()
    admin_session.add(PartyRoleGrant(tenant_id=tenant.id, party_id=person.id, role_id=roles["admin"].id))
    admin_session.commit()


def _person_with_role(admin_session, tenant, *, email, role="instructor", status="active"):
    roles = _canonical_roles(admin_session, tenant.id)
    person = _canonical_person(
        admin_session, tenant_id=tenant.id, email=email, first_name="Test", last_name="Instructor", status=status
    )
    admin_session.add(person)
    admin_session.flush()
    if role is not None:
        admin_session.add(PartyRoleGrant(tenant_id=tenant.id, party_id=person.id, role_id=roles[role].id))
    return person


def _post_facilitator(app_client, headers, cohort_id, person_id, *, scope="cohort", offering_id=None):
    csrf = app_client.cookies.get("csrf_token", "")
    return app_client.post(
        f"/instructor/cohorts/{cohort_id}/facilitators",
        headers={**headers, "x-csrf-token": csrf},
        data={
            "facilitator_id": str(person_id),
            "scope": scope,
            "offering_id": str(offering_id) if offering_id else "",
        },
        follow_redirects=False,
    )


def test_admin_course_preview_renders_imported_html_content(app_client, admin_session, tenant_a):
    headers = _login_instructor(app_client, admin_session, tenant_a)
    _grant_admin(admin_session, tenant_a)
    course = Course(
        tenant_id=tenant_a.id,
        slug="helpdesk-preview",
        title="Service Delivery & Helpdesk",
        discipline="networking",
        source_ref="imported",
        version=1,
        status="published",
        listed=False,
    )
    admin_session.add(course)
    admin_session.flush()
    admin_session.add(
        Chapter(
            tenant_id=tenant_a.id,
            course_id=course.id,
            number=1,
            title="Helpdesk Foundations",
            part="Foundations",
            body_html="<h2>Learning objectives</h2><p>Resolve customer incidents.</p>",
            source_hash="helpdesk-html",
            order_index=1,
        )
    )
    admin_session.commit()

    response = app_client.get(f"/instructor/courses/{course.id}/preview", headers=headers)

    assert response.status_code == 200
    assert "Service Delivery &amp; Helpdesk" in response.text
    assert "Helpdesk Foundations" in response.text
    assert "Learning objectives" in response.text
    assert "Resolve customer incidents." in response.text


def test_admin_nomination_preserves_student_enrollment_and_revoke(app_client, admin_session, tenant_a):
    headers = _login_instructor(app_client, admin_session, tenant_a)
    _grant_admin(admin_session, tenant_a)
    cohort = Cohort(tenant_id=tenant_a.id, name="Facilitated", discipline="networking", status="active")
    facilitator = _person_with_role(admin_session, tenant_a, email="facilitator@a.edu", role=None)
    admin_session.add(cohort)
    admin_session.flush()
    enrollment = Enrollment(
        tenant_id=tenant_a.id,
        cohort_id=cohort.id,
        person_id=facilitator.id,
        role_in_cohort="student",
        status="active",
    )
    admin_session.add(enrollment)
    admin_session.commit()

    response = _post_facilitator(app_client, headers, cohort.id, facilitator.id)
    assert response.status_code == 303
    admin_session.expire_all()
    nomination = admin_session.scalars(
        select(FacilitatorNomination)
        .where(FacilitatorNomination.tenant_id == tenant_a.id)
        .where(FacilitatorNomination.cohort_id == cohort.id)
        .where(FacilitatorNomination.person_id == facilitator.id)
    ).one()
    assert nomination.status == "pending"
    assert enrollment.role_in_cohort == "student"
    assert enrollment.status == "active"

    csrf = app_client.cookies.get("csrf_token", "")
    response = app_client.post(
        f"/instructor/facilitators/{nomination.id}/revoke",
        headers={**headers, "x-csrf-token": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 303
    admin_session.expire_all()
    assert nomination.status == "revoked"
    assert enrollment.role_in_cohort == "student"
    assert enrollment.status == "active"


def test_admin_nomination_activates_immediately_when_course_already_completed(app_client, admin_session, tenant_a):
    headers = _login_instructor(app_client, admin_session, tenant_a)
    _grant_admin(admin_session, tenant_a)
    cohort = Cohort(tenant_id=tenant_a.id, name="Qualified", discipline="networking", status="active")
    facilitator = _person_with_role(admin_session, tenant_a, email="qualified@a.edu", role=None)
    course = Course(
        tenant_id=tenant_a.id,
        slug="qualified-course",
        title="Qualified Course",
        discipline="networking",
        source_ref="test",
        version=1,
        status="published",
        listed=False,
    )
    admin_session.add_all([cohort, course])
    admin_session.flush()
    offering = CourseOffering(tenant_id=tenant_a.id, cohort_id=cohort.id, course_id=course.id, status="active")
    completion = CourseCompletion(
        tenant_id=tenant_a.id,
        person_id=facilitator.id,
        course_id=course.id,
        status="completed",
        pct=1.0,
    )
    admin_session.add_all([offering, completion])
    admin_session.commit()

    response = _post_facilitator(
        app_client, headers, cohort.id, facilitator.id, scope="course", offering_id=offering.id
    )
    assert response.status_code == 303
    admin_session.expire_all()
    nomination = admin_session.scalars(
        select(FacilitatorNomination)
        .where(FacilitatorNomination.tenant_id == tenant_a.id)
        .where(FacilitatorNomination.person_id == facilitator.id)
    ).one()
    assert nomination.status == "active"
    assert nomination.offering_id == offering.id


def test_non_admin_instructor_cannot_assign_facilitator(app_client, admin_session, tenant_a):
    headers = _login_instructor(app_client, admin_session, tenant_a)
    cohort = Cohort(tenant_id=tenant_a.id, name="Admin Only", discipline="networking", status="active")
    facilitator = _person_with_role(admin_session, tenant_a, email="peer-facilitator@a.edu")
    admin_session.add(cohort)
    admin_session.commit()

    response = _post_facilitator(app_client, headers, cohort.id, facilitator.id)
    assert response.status_code == 403
    assert (
        admin_session.query(FacilitatorNomination)
        .filter(
            FacilitatorNomination.cohort_id == cohort.id,
            FacilitatorNomination.person_id == facilitator.id,
        )
        .count()
        == 0
    )


def test_user_without_instructor_role_can_be_nominated(app_client, admin_session, tenant_a):
    headers = _login_instructor(app_client, admin_session, tenant_a)
    _grant_admin(admin_session, tenant_a)
    cohort = Cohort(tenant_id=tenant_a.id, name="Role Gate", discipline="networking", status="active")
    learner = _person_with_role(admin_session, tenant_a, email="learner-only@a.edu", role=None)
    admin_session.add(cohort)
    admin_session.commit()

    response = _post_facilitator(app_client, headers, cohort.id, learner.id)
    assert response.status_code == 303
    nomination = admin_session.scalars(
        select(FacilitatorNomination)
        .where(FacilitatorNomination.tenant_id == tenant_a.id)
        .where(FacilitatorNomination.person_id == learner.id)
    ).one()
    assert nomination.status == "pending"


def test_facilitator_assignment_preserves_tenant_isolation(app_client, admin_session, tenant_a, tenant_b):
    headers = _login_instructor(app_client, admin_session, tenant_a)
    _grant_admin(admin_session, tenant_a)
    cohort_a = Cohort(tenant_id=tenant_a.id, name="Alpha Cohort", discipline="networking", status="active")
    instructor_a = _person_with_role(admin_session, tenant_a, email="alpha-facilitator@a.edu")
    cohort_b = Cohort(tenant_id=tenant_b.id, name="Beta Cohort", discipline="networking", status="active")
    instructor_b = _person_with_role(admin_session, tenant_b, email="beta-facilitator@b.edu")
    admin_session.add_all([cohort_a, cohort_b])
    admin_session.commit()

    assert _post_facilitator(app_client, headers, cohort_a.id, instructor_b.id).status_code == 404
    assert _post_facilitator(app_client, headers, cohort_b.id, instructor_a.id).status_code == 404
    assert (
        admin_session.query(FacilitatorNomination)
        .filter(FacilitatorNomination.person_id.in_([instructor_a.id, instructor_b.id]))
        .count()
        == 0
    )


def test_instructor_can_create_cohort(app_client, admin_session, tenant_a):
    """An instructor can POST to create a cohort; the row lands in the DB."""
    h = _login_instructor(app_client, admin_session, tenant_a)

    # After login (with redirect to "/" followed), the TestClient jar has both
    # `session` and `csrf_token` cookies. Subsequent POSTs must include x-csrf-token.
    csrf = app_client.cookies.get("csrf_token", "")
    r = app_client.post(
        "/instructor/cohorts",
        headers={**h, "x-csrf-token": csrf},
        data={"name": "Abuja 2026", "discipline": "networking"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    # admin_session bypasses RLS; the Cohort committed by get_db is visible here.
    assert admin_session.query(Cohort).filter(Cohort.tenant_id == tenant_a.id).count() == 1


def test_enroll_cross_tenant_404(app_client, admin_session, tenant_a, tenant_b):
    """POSTing to enroll with a cohort owned by a different tenant returns 404."""
    h = _login_instructor(app_client, admin_session, tenant_a)

    # Create a cohort under tenant_b directly (bypasses RLS via admin_session).
    cohort_b = Cohort(tenant_id=tenant_b.id, name="Beta Cohort", discipline="security", status="active")
    admin_session.add(cohort_b)
    admin_session.commit()
    admin_session.refresh(cohort_b)

    csrf = app_client.cookies.get("csrf_token", "")
    r = app_client.post(
        f"/instructor/cohorts/{cohort_b.id}/enroll",
        headers={**h, "x-csrf-token": csrf},
        data={"email": "nobody@example.com"},
        follow_redirects=False,
    )
    assert r.status_code == 404


def test_bulk_enroll_surfaces_unknown_emails(app_client, admin_session, tenant_a):
    """Finding #6: enrolling reports unknown emails instead of silently no-oping."""
    h = _login_instructor(app_client, admin_session, tenant_a)
    # A real student + a cohort.
    stu = _canonical_person(admin_session, tenant_id=tenant_a.id, email="real@a.edu", first_name="Re", last_name="Al")
    admin_session.add(stu)
    coh = Cohort(tenant_id=tenant_a.id, name="Roster", discipline="networking", status="active")
    admin_session.add(coh)
    admin_session.commit()
    admin_session.refresh(coh)

    csrf = app_client.cookies.get("csrf_token", "")
    r = app_client.post(
        f"/instructor/cohorts/{coh.id}/enroll",
        headers={**h, "x-csrf-token": csrf, "HX-Request": "true"},
        data={"emails": "real@a.edu, ghost@a.edu"},
    )
    assert r.status_code == 200
    assert "Enrolled 1" in r.text
    assert "ghost@a.edu" in r.text  # unknown email surfaced
    from app.models.cohort import Enrollment

    n = admin_session.query(Enrollment).filter(Enrollment.cohort_id == coh.id, Enrollment.status == "active").count()
    assert n == 1


def test_enroll_route_reports_non_admin_reactivation_refusal_without_erroring(app_client, admin_session, tenant_a):
    """A plain instructor cannot type a dropped fellow instructor's email into
    the enroll form to silently restore their authoring access. This is now
    reported as a categorized outcome in the summary (matching the existing
    "Unknown (not enrolled)" pattern), NOT a raw error/retarget — and the
    enrollment stays dropped."""
    h = _login_instructor(app_client, admin_session, tenant_a)
    coh = Cohort(tenant_id=tenant_a.id, name="EnrollGate", discipline="networking", status="active")
    peer = _canonical_person(
        admin_session, tenant_id=tenant_a.id, email="peer-enroll@a.edu", first_name="Peer", last_name="Enroll"
    )
    admin_session.add_all([coh, peer])
    admin_session.flush()
    admin_session.add(
        Enrollment(
            tenant_id=tenant_a.id,
            cohort_id=coh.id,
            person_id=peer.id,
            role_in_cohort="instructor",
            status="dropped",
        )
    )
    admin_session.commit()

    csrf = app_client.cookies.get("csrf_token", "")
    r = app_client.post(
        f"/instructor/cohorts/{coh.id}/enroll",
        headers={**h, "x-csrf-token": csrf, "HX-Request": "true"},
        data={"email": "peer-enroll@a.edu"},
    )
    assert r.status_code == 200
    assert r.headers.get("HX-Retarget") is None
    assert "Requires admin to reactivate" in r.text
    assert "peer-enroll@a.edu" in r.text
    admin_session.expire_all()
    enr = admin_session.scalars(
        select(Enrollment).where(Enrollment.cohort_id == coh.id).where(Enrollment.person_id == peer.id)
    ).first()
    assert enr.status == "dropped"  # unchanged


def test_enroll_route_processes_rest_of_batch_after_a_refused_reactivation(app_client, admin_session, tenant_a):
    """A refused reactivation for one email in a multi-email submission must
    not abort the other emails in the same batch — bulk_enroll's per-email
    outcome contract (like "not_found") continues processing regardless."""
    h = _login_instructor(app_client, admin_session, tenant_a)
    coh = Cohort(tenant_id=tenant_a.id, name="EnrollBatch", discipline="networking", status="active")
    peer = _canonical_person(
        admin_session, tenant_id=tenant_a.id, email="peer-batch@a.edu", first_name="Peer", last_name="Batch"
    )
    fresh = _canonical_person(
        admin_session, tenant_id=tenant_a.id, email="fresh-batch@a.edu", first_name="Fresh", last_name="Batch"
    )
    admin_session.add_all([coh, peer, fresh])
    admin_session.flush()
    admin_session.add(
        Enrollment(
            tenant_id=tenant_a.id,
            cohort_id=coh.id,
            person_id=peer.id,
            role_in_cohort="instructor",
            status="dropped",
        )
    )
    admin_session.commit()

    csrf = app_client.cookies.get("csrf_token", "")
    r = app_client.post(
        f"/instructor/cohorts/{coh.id}/enroll",
        headers={**h, "x-csrf-token": csrf, "HX-Request": "true"},
        data={"emails": "peer-batch@a.edu, fresh-batch@a.edu"},
    )
    assert r.status_code == 200
    assert "Enrolled 1." in r.text
    assert "Requires admin to reactivate" in r.text
    assert "peer-batch@a.edu" in r.text
    admin_session.expire_all()
    peer_enr = admin_session.scalars(
        select(Enrollment).where(Enrollment.cohort_id == coh.id).where(Enrollment.person_id == peer.id)
    ).first()
    fresh_enr = admin_session.scalars(
        select(Enrollment).where(Enrollment.cohort_id == coh.id).where(Enrollment.person_id == fresh.id)
    ).first()
    assert peer_enr.status == "dropped"  # unchanged, refused
    assert fresh_enr is not None and fresh_enr.status == "active"  # rest of the batch still committed


def test_enroll_route_summary_separates_enrolled_from_reactivated(app_client, admin_session, tenant_a):
    """The enroll summary line must not fold reactivations into "Enrolled N"."""
    h = _login_instructor(app_client, admin_session, tenant_a)
    coh = Cohort(tenant_id=tenant_a.id, name="EnrollSummary", discipline="networking", status="active")
    fresh = _canonical_person(
        admin_session, tenant_id=tenant_a.id, email="fresh-enroll@a.edu", first_name="Fresh", last_name="Enroll"
    )
    dropped = _canonical_person(
        admin_session, tenant_id=tenant_a.id, email="dropped-enroll@a.edu", first_name="Dropped", last_name="Enroll"
    )
    admin_session.add_all([coh, fresh, dropped])
    admin_session.flush()
    admin_session.add(
        Enrollment(
            tenant_id=tenant_a.id,
            cohort_id=coh.id,
            person_id=dropped.id,
            role_in_cohort="student",
            status="dropped",
        )
    )
    admin_session.commit()

    csrf = app_client.cookies.get("csrf_token", "")
    r = app_client.post(
        f"/instructor/cohorts/{coh.id}/enroll",
        headers={**h, "x-csrf-token": csrf, "HX-Request": "true"},
        data={"emails": "fresh-enroll@a.edu, dropped-enroll@a.edu"},
    )
    assert r.status_code == 200
    assert "Enrolled 1." in r.text
    assert "Reactivated 1." in r.text


def test_invite_route_refuses_non_admin_reactivating_instructor_enrollment(app_client, admin_session, tenant_a):
    """Same gap, different door: the invite form must not let a plain
    instructor reactivate a dropped fellow instructor's enrollment either."""
    h = _login_instructor(app_client, admin_session, tenant_a)
    coh = Cohort(tenant_id=tenant_a.id, name="InviteGate", discipline="networking", status="active")
    peer = _canonical_person(
        admin_session, tenant_id=tenant_a.id, email="peer-invite@a.edu", first_name="Peer", last_name="Invite"
    )
    admin_session.add_all([coh, peer])
    admin_session.flush()
    admin_session.add(
        Enrollment(
            tenant_id=tenant_a.id,
            cohort_id=coh.id,
            person_id=peer.id,
            role_in_cohort="instructor",
            status="dropped",
        )
    )
    admin_session.commit()

    csrf = app_client.cookies.get("csrf_token", "")
    r = app_client.post(
        f"/instructor/cohorts/{coh.id}/invite",
        headers={**h, "x-csrf-token": csrf, "HX-Request": "true"},
        data={"email": "peer-invite@a.edu"},
    )
    assert r.status_code == 200
    assert r.headers.get("HX-Retarget") == f"#invite-result-{coh.id}"
    admin_session.expire_all()
    enr = admin_session.scalars(
        select(Enrollment).where(Enrollment.cohort_id == coh.id).where(Enrollment.person_id == peer.id)
    ).first()
    assert enr.status == "dropped"  # unchanged


def test_invite_route_surfaces_reactivation_description(app_client, admin_session, tenant_a):
    """invite_to_cohort's response must render _apply_assignment's
    description text (e.g. "reactivated (was dropped)"), not discard it."""
    h = _login_instructor(app_client, admin_session, tenant_a)
    coh = Cohort(tenant_id=tenant_a.id, name="InviteVisible", discipline="networking", status="active")
    stu = _canonical_person(
        admin_session, tenant_id=tenant_a.id, email="invite-reactivate@a.edu", first_name="Re", last_name="Activate"
    )
    admin_session.add_all([coh, stu])
    admin_session.flush()
    admin_session.add(
        Enrollment(
            tenant_id=tenant_a.id,
            cohort_id=coh.id,
            person_id=stu.id,
            role_in_cohort="student",
            status="dropped",
        )
    )
    admin_session.commit()

    csrf = app_client.cookies.get("csrf_token", "")
    r = app_client.post(
        f"/instructor/cohorts/{coh.id}/invite",
        headers={**h, "x-csrf-token": csrf, "HX-Request": "true"},
        data={"email": "invite-reactivate@a.edu"},
    )
    assert r.status_code == 200
    assert "reactivated (was dropped)" in r.text


def test_grading_queue_lists_pending(app_client, admin_session, tenant_a):
    """Finding #4: manual submissions awaiting a score appear in the queue."""
    from app.models.assessment import Activity, Submission
    from app.models.course import Course

    h = _login_instructor(app_client, admin_session, tenant_a)
    learner = _canonical_person(
        admin_session, tenant_id=tenant_a.id, email="learner@a.edu", first_name="Le", last_name="Ar"
    )
    course = Course(tenant_id=tenant_a.id, slug="mg", title="MG", discipline="networking", source_ref="x", version=1, status="published", listed=False)
    admin_session.add_all([learner, course])
    admin_session.flush()
    act = Activity(
        tenant_id=tenant_a.id,
        course_id=course.id,
        chapter_number=1,
        type="mcq_test",
        title="Essay Q",
        pass_threshold=0.6,
        grading="manual", weight=1.0,
    )
    admin_session.add(act)
    admin_session.flush()
    admin_session.add(
        Submission(tenant_id=tenant_a.id, activity_id=act.id, person_id=learner.id, answers={}, attempt_no=1)
    )  # no Score = pending
    admin_session.commit()

    r = app_client.get("/instructor/grading", headers=h)
    assert r.status_code == 200
    assert "Essay Q" in r.text
    assert "learner@a.edu" in r.text


def test_item_analytics_page(app_client, admin_session, tenant_a):
    """Finding #4/#9: per-question difficulty page renders p-values."""
    from app.models.assessment import Activity, Score, Submission
    from app.models.course import Course

    h = _login_instructor(app_client, admin_session, tenant_a)
    learner = _canonical_person(admin_session, tenant_id=tenant_a.id, email="il@a.edu", first_name="Il", last_name="A")
    course = Course(tenant_id=tenant_a.id, slug="ia", title="IA", discipline="networking", source_ref="x", version=1, status="published", listed=False)
    admin_session.add_all([learner, course])
    admin_session.flush()
    act = Activity(
        tenant_id=tenant_a.id,
        course_id=course.id,
        chapter_number=1,
        type="mcq_test",
        title="Analytics Quiz",
        pass_threshold=0.6, grading="auto", weight=1.0,
    )
    admin_session.add(act)
    admin_session.flush()
    sub = Submission(tenant_id=tenant_a.id, activity_id=act.id, person_id=learner.id, answers={}, attempt_no=1)
    admin_session.add(sub)
    admin_session.flush()
    admin_session.add(
        Score(
            tenant_id=tenant_a.id,
            submission_id=sub.id,
            score=10,
            max_score=10,
            fraction=1.0,
            passed=True,
            source="auto",
            per_item=[{"id": "q1", "correct": True}],
        )
    )
    admin_session.commit()

    r = app_client.get(f"/instructor/items/{act.id}", headers=h)
    assert r.status_code == 200
    assert "Analytics Quiz" in r.text
    assert "q1" in r.text


def test_authoring_create_course_and_chapter(app_client, admin_session, tenant_a):
    """Finding #8: instructor creates a draft course and authors a chapter in markdown."""
    from app.models.course import Chapter, Course

    h = _login_instructor(app_client, admin_session, tenant_a)
    _grant_admin(admin_session, tenant_a)
    csrf = app_client.cookies.get("csrf_token", "")
    # Create a draft course.
    r = app_client.post(
        "/instructor/courses",
        headers={**h, "x-csrf-token": csrf, "HX-Request": "true"},
        data={"slug": "authored", "title": "Authored", "discipline": "networking"},
    )
    assert r.status_code == 200
    course = admin_session.scalars(
        __import__("sqlalchemy").select(Course).where(Course.tenant_id == tenant_a.id).where(Course.slug == "authored")
    ).first()
    assert course is not None and course.status == "draft"

    # Author a chapter in markdown.
    r2 = app_client.post(
        f"/instructor/courses/{course.id}/chapters",
        headers={**h, "x-csrf-token": csrf, "HX-Request": "true"},
        data={"number": "1", "title": "Intro", "body_md": "# Welcome"},
    )
    assert r2.status_code == 200
    ch = admin_session.scalars(
        __import__("sqlalchemy").select(Chapter).where(Chapter.course_id == course.id).where(Chapter.number == 1)
    ).first()
    assert ch is not None
    assert "<h1>Welcome</h1>" in ch.body_html
    assert ch.body_md == "# Welcome"


def test_publish_course_toggles_status(app_client, admin_session, tenant_a):
    """Finding #8: instructor can publish/unpublish a course."""
    from app.models.course import Course

    h = _login_instructor(app_client, admin_session, tenant_a)
    _grant_admin(admin_session, tenant_a)
    course = Course(
        tenant_id=tenant_a.id,
        slug="pub",
        title="Pub",
        discipline="networking",
        source_ref="x",
        version=1,
        status="draft",
        listed=False,
    )
    admin_session.add(course)
    admin_session.commit()
    admin_session.refresh(course)

    csrf = app_client.cookies.get("csrf_token", "")
    r = app_client.post(
        f"/instructor/courses/{course.id}/status",
        headers={**h, "x-csrf-token": csrf, "HX-Request": "true"},
        data={"status_value": "published"},
    )
    assert r.status_code == 200
    admin_session.expire(course)
    assert admin_session.get(Course, course.id).status == "published"


def test_student_forbidden(app_client, admin_session, tenant_a):
    """A user with only the student role gets 403 on any instructor-gated route."""
    roles = _canonical_roles(admin_session, tenant_a.id)
    p = _canonical_person(admin_session, tenant_id=tenant_a.id, email="s3@a.edu", first_name="S", last_name="T")
    admin_session.add(p)
    admin_session.flush()
    admin_session.add(
        _canonical_credential(
            admin_session,
            tenant_id=tenant_a.id,
            party_id=p.id,
            email="s3@a.edu",
            password_hash=hash_password("password1"),
        )
    )
    admin_session.add(PartyRoleGrant(tenant_id=tenant_a.id, party_id=p.id, role_id=roles["student"].id))
    admin_session.commit()

    h = {"Host": "alpha.localhost"}
    web_login(app_client, h, "s3@a.edu", "password1")
    assert app_client.get("/instructor/results", headers=h).status_code == 403


def test_create_track_error_renders_inline_message_not_raw_400(app_client, admin_session, tenant_a):
    """Finding #1: create_cohort_track's BadRequestError ("select at least one
    course") must render inline (200 + retarget), not become a raw 400 that
    htmx cannot swap into hx-target="body"."""
    h = _login_instructor(app_client, admin_session, tenant_a)
    coh = Cohort(tenant_id=tenant_a.id, name="Errs", discipline="networking", status="active")
    admin_session.add(coh)
    admin_session.commit()
    admin_session.refresh(coh)

    csrf = app_client.cookies.get("csrf_token", "")
    r = app_client.post(
        f"/instructor/cohorts/{coh.id}/tracks",
        headers={**h, "x-csrf-token": csrf, "HX-Request": "true"},
        data={"name": "Empty Track"},  # no course_ids selected -> BadRequestError
    )
    assert r.status_code == 200
    assert "select at least one course" in r.text
    assert r.headers.get("HX-Retarget") == f"#track-form-error-{coh.id}"
    # No track was actually created.
    from app.models.track import Track

    assert admin_session.query(Track).filter(Track.tenant_id == tenant_a.id, Track.name == "Empty Track").count() == 0


def test_add_courses_to_track_error_renders_inline_message(app_client, admin_session, tenant_a):
    """Finding #1: add_courses_to_cohort_track's BadRequestError renders inline."""
    h = _login_instructor(app_client, admin_session, tenant_a)
    coh = Cohort(tenant_id=tenant_a.id, name="Errs2", discipline="networking", status="active")
    course = Course(
        tenant_id=tenant_a.id,
        slug="c1",
        title="C1",
        discipline="networking",
        source_ref="x",
        version=1,
        status="published",
        listed=False,
    )
    admin_session.add_all([coh, course])
    admin_session.flush()
    track = create_cohort_track(
        admin_session, tenant_id=tenant_a.id, cohort_id=coh.id, name="T1", course_ids=[course.id]
    )
    admin_session.commit()
    from app.models.track import Track

    track = admin_session.get(Track, track.id)

    csrf = app_client.cookies.get("csrf_token", "")
    r = app_client.post(
        f"/instructor/cohorts/{coh.id}/tracks/{track.id}/courses",
        headers={**h, "x-csrf-token": csrf, "HX-Request": "true"},
        data={},  # no course_ids selected -> BadRequestError
    )
    assert r.status_code == 200
    assert "select at least one course" in r.text
    assert r.headers.get("HX-Retarget") == f"#track-courses-error-{track.id}"


def test_roster_track_reassign_error_renders_inline_message(app_client, admin_session, tenant_a):
    """Finding #1: assign_enrollment_track's NotFoundError (unknown track) renders inline."""
    h = _login_instructor(app_client, admin_session, tenant_a)
    coh = Cohort(tenant_id=tenant_a.id, name="Errs3", discipline="networking", status="active")
    stu = _canonical_person(admin_session, tenant_id=tenant_a.id, email="stu3@a.edu", first_name="S", last_name="T")
    admin_session.add_all([coh, stu])
    admin_session.flush()
    admin_session.add(
        Enrollment(tenant_id=tenant_a.id, cohort_id=coh.id, person_id=stu.id, role_in_cohort="student", status="active")
    )
    admin_session.commit()

    bad_track_id = uuid4()
    csrf = app_client.cookies.get("csrf_token", "")
    r = app_client.post(
        f"/instructor/cohorts/{coh.id}/roster/{stu.id}/track",
        headers={**h, "x-csrf-token": csrf, "HX-Request": "true"},
        data={"track_id": str(bad_track_id)},
    )
    assert r.status_code == 200
    assert "not found" in r.text.lower()
    assert r.headers.get("HX-Retarget") == f"#roster-track-error-{coh.id}-{stu.id}"


def test_draft_courses_excluded_from_track_checkbox_lists(app_client, admin_session, tenant_a):
    """Finding #2: a draft course is hidden from the create-track/add-courses
    checkbox lists (drafts grant nothing per entitlements.py); a published one
    is shown, and the hidden-draft-count note appears."""
    h = _login_instructor(app_client, admin_session, tenant_a)
    coh = Cohort(tenant_id=tenant_a.id, name="DraftTest", discipline="networking", status="active")
    draft = Course(
        tenant_id=tenant_a.id,
        slug="draft-c",
        title="Draft Course Title",
        discipline="networking",
        source_ref="x",
        version=1,
        status="draft",
        listed=False,
    )
    published = Course(
        tenant_id=tenant_a.id,
        slug="pub-c",
        title="Published Course Title",
        discipline="networking",
        source_ref="x",
        version=1,
        status="published",
        listed=False,
    )
    admin_session.add_all([coh, draft, published])
    admin_session.commit()

    r = app_client.get("/instructor/cohorts", headers=h)
    assert r.status_code == 200
    assert "Published Course Title" in r.text
    assert "Draft Course Title" not in r.text
    assert "draft course(s) hidden" in r.text


def test_roster_shows_dropped_and_waitlisted_enrollments(app_client, admin_session, tenant_a):
    """Finding #3: the roster table no longer hides non-active-student rows."""
    h = _login_instructor(app_client, admin_session, tenant_a)
    coh = Cohort(tenant_id=tenant_a.id, name="RosterTest", discipline="networking", status="active")
    dropped_p = _canonical_person(
        admin_session, tenant_id=tenant_a.id, email="dropped@a.edu", first_name="Drop", last_name="Ped"
    )
    waitlisted_p = _canonical_person(
        admin_session, tenant_id=tenant_a.id, email="wait@a.edu", first_name="Wait", last_name="List"
    )
    admin_session.add_all([coh, dropped_p, waitlisted_p])
    admin_session.flush()
    admin_session.add(
        Enrollment(
            tenant_id=tenant_a.id,
            cohort_id=coh.id,
            person_id=dropped_p.id,
            role_in_cohort="student",
            status="dropped",
        )
    )
    admin_session.add(
        Enrollment(
            tenant_id=tenant_a.id,
            cohort_id=coh.id,
            person_id=waitlisted_p.id,
            role_in_cohort="student",
            status="waitlisted",
        )
    )
    admin_session.commit()

    r = app_client.get("/instructor/cohorts", headers=h)
    assert r.status_code == 200
    assert "dropped@a.edu" in r.text
    assert "wait@a.edu" in r.text


def test_non_admin_instructor_forbidden_from_changing_instructor_role_enrollment(app_client, admin_session, tenant_a):
    """Finding #3 authorization: a plain instructor cannot drop/reactivate a
    non-student (e.g. instructor-role) enrollment — that is admin-only,
    because dropping it silently strips that instructor's own authoring
    access via _assigned_course_ids."""
    h = _login_instructor(app_client, admin_session, tenant_a)
    coh = Cohort(tenant_id=tenant_a.id, name="AuthzTest", discipline="networking", status="active")
    other_instructor = _canonical_person(
        admin_session, tenant_id=tenant_a.id, email="peer@a.edu", first_name="Peer", last_name="Instr"
    )
    admin_session.add_all([coh, other_instructor])
    admin_session.flush()
    admin_session.add(
        Enrollment(
            tenant_id=tenant_a.id,
            cohort_id=coh.id,
            person_id=other_instructor.id,
            role_in_cohort="instructor",
            status="active",
        )
    )
    admin_session.commit()

    csrf = app_client.cookies.get("csrf_token", "")
    r = app_client.post(
        f"/instructor/cohorts/{coh.id}/roster/{other_instructor.id}/state",
        headers={**h, "x-csrf-token": csrf},
        data={"state": "dropped"},
        follow_redirects=False,
    )
    assert r.status_code == 403
    admin_session.expire_all()
    enr = admin_session.scalars(
        select(Enrollment).where(Enrollment.cohort_id == coh.id).where(Enrollment.person_id == other_instructor.id)
    ).first()
    assert enr.status == "active"  # unchanged


def test_admin_can_change_instructor_role_enrollment_state(app_client, admin_session, tenant_a):
    """Finding #3 authorization: an admin CAN drop/reactivate a non-student enrollment."""
    h = _login_instructor(app_client, admin_session, tenant_a)
    _grant_admin(admin_session, tenant_a)
    coh = Cohort(tenant_id=tenant_a.id, name="AuthzTest2", discipline="networking", status="active")
    other_instructor = _canonical_person(
        admin_session, tenant_id=tenant_a.id, email="peer2@a.edu", first_name="Peer", last_name="Instr"
    )
    admin_session.add_all([coh, other_instructor])
    admin_session.flush()
    admin_session.add(
        Enrollment(
            tenant_id=tenant_a.id,
            cohort_id=coh.id,
            person_id=other_instructor.id,
            role_in_cohort="instructor",
            status="active",
        )
    )
    admin_session.commit()

    csrf = app_client.cookies.get("csrf_token", "")
    r = app_client.post(
        f"/instructor/cohorts/{coh.id}/roster/{other_instructor.id}/state",
        headers={**h, "x-csrf-token": csrf},
        data={"state": "dropped"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    admin_session.expire_all()
    enr = admin_session.scalars(
        select(Enrollment).where(Enrollment.cohort_id == coh.id).where(Enrollment.person_id == other_instructor.id)
    ).first()
    assert enr.status == "dropped"


def test_reorder_track_courses_route_updates_order(app_client, admin_session, tenant_a):
    """1b: an instructor can reorder an EXISTING track's courses via the
    track-level route (not nested under a specific cohort)."""
    from app.models.track import TrackCourse

    h = _login_instructor(app_client, admin_session, tenant_a)
    coh = Cohort(tenant_id=tenant_a.id, name="ReorderRoute", discipline="networking", status="active")
    course_a = Course(
        tenant_id=tenant_a.id,
        slug="rr-a",
        title="RR A",
        discipline="networking",
        source_ref="x",
        version=1,
        status="published",
        listed=False,
    )
    course_b = Course(
        tenant_id=tenant_a.id,
        slug="rr-b",
        title="RR B",
        discipline="networking",
        source_ref="x",
        version=1,
        status="published",
        listed=False,
    )
    admin_session.add_all([coh, course_a, course_b])
    admin_session.flush()
    track = create_cohort_track(
        admin_session,
        tenant_id=tenant_a.id,
        cohort_id=coh.id,
        name="Route Track",
        course_ids=[course_a.id, course_b.id],
    )
    admin_session.commit()
    from app.models.track import Track

    track = admin_session.get(Track, track.id)

    csrf = app_client.cookies.get("csrf_token", "")
    r = app_client.post(
        f"/instructor/tracks/{track.id}/reorder",
        headers={**h, "x-csrf-token": csrf},
        data={
            "course_ids": [str(course_a.id), str(course_b.id)],
            f"position_{course_b.id}": "1",
            f"position_{course_a.id}": "2",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    admin_session.expire_all()
    rows = admin_session.scalars(
        select(TrackCourse).where(TrackCourse.track_id == track.id).order_by(TrackCourse.order_index)
    ).all()
    assert [row.course_id for row in rows] == [course_b.id, course_a.id]


def test_reorder_track_courses_route_rejects_mismatched_membership(app_client, admin_session, tenant_a):
    """1b: reordering with an id that isn't in the track renders inline,
    same as the other track-form BadRequestError handlers."""
    h = _login_instructor(app_client, admin_session, tenant_a)
    coh = Cohort(tenant_id=tenant_a.id, name="ReorderReject", discipline="networking", status="active")
    course = Course(
        tenant_id=tenant_a.id,
        slug="rr-only",
        title="RR Only",
        discipline="networking",
        source_ref="x",
        version=1,
        status="published",
        listed=False,
    )
    admin_session.add_all([coh, course])
    admin_session.flush()
    track = create_cohort_track(
        admin_session, tenant_id=tenant_a.id, cohort_id=coh.id, name="Reject Route Track", course_ids=[course.id]
    )
    admin_session.commit()
    from app.models.track import Track

    track = admin_session.get(Track, track.id)

    csrf = app_client.cookies.get("csrf_token", "")
    r = app_client.post(
        f"/instructor/tracks/{track.id}/reorder",
        headers={**h, "x-csrf-token": csrf, "HX-Request": "true"},
        data={"course_ids": [str(uuid4())]},  # not a member of the track
    )
    assert r.status_code == 200
    assert "must match" in r.text.lower()
    assert r.headers.get("HX-Retarget") == f"#track-reorder-error-{track.id}"


def test_clear_roster_track_forbidden_for_non_admin_instructor(app_client, admin_session, tenant_a):
    """1d: clearing a learner's track is admin-only, regardless of the
    target's role (mirrors change_roster_state's admin gate)."""
    h = _login_instructor(app_client, admin_session, tenant_a)
    coh = Cohort(tenant_id=tenant_a.id, name="ClearForbidden", discipline="networking", status="active")
    course = Course(
        tenant_id=tenant_a.id,
        slug="clear-c",
        title="Clear C",
        discipline="networking",
        source_ref="x",
        version=1,
        status="published",
        listed=False,
    )
    stu = _canonical_person(
        admin_session, tenant_id=tenant_a.id, email="clear-stu@a.edu", first_name="C", last_name="S"
    )
    admin_session.add_all([coh, course, stu])
    admin_session.flush()
    track = create_cohort_track(
        admin_session, tenant_id=tenant_a.id, cohort_id=coh.id, name="Clear Track", course_ids=[course.id]
    )
    admin_session.add(
        Enrollment(
            tenant_id=tenant_a.id,
            cohort_id=coh.id,
            person_id=stu.id,
            role_in_cohort="student",
            status="active",
            track_id=track.id,
        )
    )
    admin_session.commit()

    csrf = app_client.cookies.get("csrf_token", "")
    r = app_client.post(
        f"/instructor/cohorts/{coh.id}/roster/{stu.id}/track/clear",
        headers={**h, "x-csrf-token": csrf},
        follow_redirects=False,
    )
    assert r.status_code == 403
    admin_session.expire_all()
    enr = admin_session.scalars(
        select(Enrollment).where(Enrollment.cohort_id == coh.id).where(Enrollment.person_id == stu.id)
    ).first()
    assert enr.track_id == track.id  # unchanged


def test_admin_can_clear_a_learners_stuck_track(app_client, admin_session, tenant_a):
    """1d: an admin clearing a learner's track nulls Enrollment.track_id
    (not a fabricated completion) and falls back to cohort-wide access."""
    h = _login_instructor(app_client, admin_session, tenant_a)
    _grant_admin(admin_session, tenant_a)
    coh = Cohort(tenant_id=tenant_a.id, name="ClearAdmin", discipline="networking", status="active")
    course = Course(
        tenant_id=tenant_a.id,
        slug="clear-a-c",
        title="Clear Admin C",
        discipline="networking",
        source_ref="x",
        version=1,
        status="published",
        listed=False,
    )
    stu = _canonical_person(
        admin_session, tenant_id=tenant_a.id, email="clear-admin-stu@a.edu", first_name="C", last_name="A"
    )
    admin_session.add_all([coh, course, stu])
    admin_session.flush()
    track = create_cohort_track(
        admin_session, tenant_id=tenant_a.id, cohort_id=coh.id, name="Clear Admin Track", course_ids=[course.id]
    )
    admin_session.add(
        Enrollment(
            tenant_id=tenant_a.id,
            cohort_id=coh.id,
            person_id=stu.id,
            role_in_cohort="student",
            status="active",
            track_id=track.id,
        )
    )
    admin_session.commit()

    csrf = app_client.cookies.get("csrf_token", "")
    r = app_client.post(
        f"/instructor/cohorts/{coh.id}/roster/{stu.id}/track/clear",
        headers={**h, "x-csrf-token": csrf},
        follow_redirects=False,
    )
    assert r.status_code == 303
    admin_session.expire_all()
    enr = admin_session.scalars(
        select(Enrollment).where(Enrollment.cohort_id == coh.id).where(Enrollment.person_id == stu.id)
    ).first()
    assert enr.track_id is None


def test_clear_roster_track_error_renders_inline_message(app_client, admin_session, tenant_a):
    """1d: NotFoundError (no enrollment) renders inline, same as the other
    roster-action handlers."""
    h = _login_instructor(app_client, admin_session, tenant_a)
    _grant_admin(admin_session, tenant_a)
    coh = Cohort(tenant_id=tenant_a.id, name="ClearMissing", discipline="networking", status="active")
    stu = _canonical_person(
        admin_session, tenant_id=tenant_a.id, email="clear-missing@a.edu", first_name="C", last_name="M"
    )
    admin_session.add_all([coh, stu])
    admin_session.commit()

    csrf = app_client.cookies.get("csrf_token", "")
    r = app_client.post(
        f"/instructor/cohorts/{coh.id}/roster/{stu.id}/track/clear",
        headers={**h, "x-csrf-token": csrf, "HX-Request": "true"},
    )
    assert r.status_code == 200
    assert "not found" in r.text.lower()
    assert r.headers.get("HX-Retarget") == f"#roster-track-clear-error-{coh.id}-{stu.id}"


def test_cohorts_page_shows_zero_activity_course_warning(app_client, admin_session, tenant_a):
    """1c: a zero-Activity course in a track shows a live-computed warning,
    without blocking the track from rendering."""
    from app.models.assessment import Activity

    h = _login_instructor(app_client, admin_session, tenant_a)
    coh = Cohort(tenant_id=tenant_a.id, name="ActivityWarning", discipline="networking", status="active")
    course = Course(
        tenant_id=tenant_a.id,
        slug="warn-c",
        title="Warn Course",
        discipline="networking",
        source_ref="x",
        version=1,
        status="published",
        listed=False,
    )
    admin_session.add_all([coh, course])
    admin_session.flush()
    create_cohort_track(
        admin_session, tenant_id=tenant_a.id, cohort_id=coh.id, name="Warn Track", course_ids=[course.id]
    )
    admin_session.commit()

    r = app_client.get("/instructor/cohorts", headers=h)
    assert r.status_code == 200
    assert "no activities yet" in r.text.lower()

    # Adding an activity clears the warning on the next render (computed live).
    act = Activity(
        tenant_id=tenant_a.id, course_id=course.id, chapter_number=1, type="mcq_test", title="Q", pass_threshold=0.6, grading="auto", weight=1.0
    )
    admin_session.add(act)
    admin_session.commit()
    r2 = app_client.get("/instructor/cohorts", headers=h)
    assert "no activities yet" not in r2.text.lower()


def test_roster_state_change_error_renders_inline_message(app_client, admin_session, tenant_a):
    """Finding #1: set_roster_state's NotFoundError (invalid state) renders inline."""
    h = _login_instructor(app_client, admin_session, tenant_a)
    coh = Cohort(tenant_id=tenant_a.id, name="Errs4", discipline="networking", status="active")
    stu = _canonical_person(admin_session, tenant_id=tenant_a.id, email="stu4@a.edu", first_name="S", last_name="U")
    admin_session.add_all([coh, stu])
    admin_session.flush()
    admin_session.add(
        Enrollment(tenant_id=tenant_a.id, cohort_id=coh.id, person_id=stu.id, role_in_cohort="student", status="active")
    )
    admin_session.commit()

    csrf = app_client.cookies.get("csrf_token", "")
    r = app_client.post(
        f"/instructor/cohorts/{coh.id}/roster/{stu.id}/state",
        headers={**h, "x-csrf-token": csrf, "HX-Request": "true"},
        data={"state": "bogus-state"},
    )
    assert r.status_code == 200
    assert "invalid roster state" in r.text.lower()
    assert r.headers.get("HX-Retarget") == f"#roster-state-error-{coh.id}-{stu.id}"


def _genuine_roster_route_case(roster_actors, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from sqlalchemy import event
    from sqlalchemy.sql import Select

    from app.services.web_auth import require_web_user
    from app.web import instructor

    a = roster_actors.account
    cohort = Cohort(tenant_id=a.tid, name="Route context", discipline="networking", status="active")
    a.admin.add(cohort)
    a.admin.commit()
    cohort_id = cohort.id
    request = roster_actors.admin.request
    actor = require_web_user(request, a.db)
    actual_admin = instructor._is_admin
    admin = Mock(wraps=actual_admin)
    monkeypatch.setattr(instructor, "_is_admin", admin)
    for method in ("scalars", "add", "delete", "flush", "commit", "rollback"):
        monkeypatch.setattr(a.db, method, Mock(wraps=getattr(a.db, method)))
    reads, cohort_reads = [], []

    def observe(conn, cursor, statement, parameters, context, executemany):
        compiled = getattr(context, "compiled", None)
        selection = getattr(compiled, "statement", None)
        if isinstance(selection, Select):
            sources = selection.get_final_froms()
            if any(source.is_derived_from(Enrollment.__table__) for source in sources):
                reads.append(statement)
            if any(source.is_derived_from(Cohort.__table__) for source in sources):
                cohort_reads.append(statement)

    event.listen(a.db.get_bind(), "before_cursor_execute", observe)
    return SimpleNamespace(
        route=instructor,
        tenant=a.tenant,
        actor=actor,
        admin=admin,
        actual_admin=actual_admin,
        cohort_id=cohort_id,
        facilitator_id=a.pid,
        person_id=a.pid,
        db=a.db,
        request=request,
        actors=roster_actors,
        enrollment_reads=reads,
        cohort_reads=cohort_reads,
        observer=observe,
    )


def _removal_request(case, *, htmx=False):
    from starlette.requests import Request

    headers = list(case.request.scope["headers"])
    if htmx:
        headers.append((b"hx-request", b"true"))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/instructor/cohorts/removal",
            "query_string": b"actor_is_admin=true&tenant_id=untrusted",
            "state": {"tenant": case.tenant},
            "headers": headers,
        }
    )


def _use_route_actor(case, actor):
    from app.services.web_auth import require_web_user

    case.request = actor.request
    case.actor = require_web_user(actor.request, case.db)


def _state_target(case, role):
    if role is None:
        return None
    db = case.actors.account.admin
    row = Enrollment(
        tenant_id=case.tenant.id,
        cohort_id=case.cohort_id,
        person_id=case.person_id,
        role_in_cohort=role,
        status="active",
    )
    db.add(row)
    db.commit()
    return row


# Retired with the enrollment-as-facilitator writer (#188, plan §3.2 roster row):
# test_facilitator_removal_forwards_authenticated_scope_and_keeps_redirect,
# test_facilitator_removal_admin_denial_precedes_owner_and_cohort_sql,
# test_facilitator_removal_preserves_owner_missing_reference_detail,
# test_facilitator_removal_maps_owner_forbidden_without_redirect,
# test_facilitator_removal_required_owner_failure_propagates, and
# test_facilitator_service_forwards_authority_and_preserves_owner_result tested
# roster.assign_facilitator/remove_facilitator, deleted in C3. Their authority and
# error-mapping intent is re-proved against the nomination lifecycle by
# test_admin_nomination_preserves_student_enrollment_and_revoke (web revoke route,
# admin gate, redirect) and tests/services/test_facilitator_nominations.py
# (activate/revoke/sweep semantics, tenant isolation, idempotent revoke).



@pytest.fixture
def roster_state_route(roster_actors, monkeypatch):
    from sqlalchemy import event

    case = _genuine_roster_route_case(roster_actors, monkeypatch)
    try:
        yield case
    finally:
        event.remove(case.db.get_bind(), "before_cursor_execute", case.observer)


@pytest.mark.parametrize("initial_role", [None, "student", "instructor"])
@pytest.mark.parametrize("workspace", [False, True])
def test_roster_state_route_preserves_shortcircuit_scope_authority_and_redirect(
    roster_state_route, monkeypatch, initial_role, workspace
):
    import dotmac_learning as learning

    case = roster_state_route
    _state_target(case, initial_role)
    seen = []

    def admin_lookup(*_args):
        assert len(case.enrollment_reads) == 1
        return case.actual_admin(*_args)

    case.admin.side_effect = admin_lookup

    def owner(db, command, *, authority):
        seen.append((db, command, authority))
        assert len(case.enrollment_reads) == 1
        assert case.admin.call_count == (1 if initial_role == "instructor" else 0)
        return object()

    monkeypatch.setattr(learning, "set_roster_state", owner)
    return_to = f"/instructor/cohorts/{case.cohort_id}" if workspace else "https://untrusted.invalid/"
    response = case.route.change_roster_state(
        case.cohort_id,
        case.person_id,
        _removal_request(case),
        state="waitlisted",
        return_to=return_to,
        person=case.actor,
        db=case.db,
    )
    assert seen == [
        (
            case.db,
            learning.RosterStateCommand(
                tenant_id=case.tenant.id, cohort_id=case.cohort_id, person_id=case.person_id, state="waitlisted"
            ),
            learning.RosterAuthority(tenant_id=case.tenant.id, actor_is_admin=True),
        )
    ]
    assert response.status_code == 303
    assert response.headers["location"] == (return_to if workspace else "/instructor/cohorts")
    if initial_role == "instructor":
        case.admin.assert_called_once_with(case.db, case.tenant.id, case.actor.id)
    else:
        case.admin.assert_not_called()
    case.db.flush.assert_called()


def test_roster_state_nonadmin_nonstudent_refuses_before_delegate(roster_state_route, monkeypatch):
    from unittest.mock import Mock

    import dotmac_learning as learning
    from fastapi import HTTPException

    case = roster_state_route
    _state_target(case, "instructor")
    _use_route_actor(case, case.actors.instructor)
    owner = Mock()
    monkeypatch.setattr(learning, "set_roster_state", owner)
    with pytest.raises(HTTPException) as caught:
        case.route.change_roster_state(
            case.cohort_id,
            case.person_id,
            _removal_request(case),
            state="bogus",
            return_to="",
            person=case.actor,
            db=case.db,
        )
    assert caught.value.status_code == 403 and caught.value.detail == "Forbidden"
    assert len(case.enrollment_reads) == 1
    owner.assert_not_called()


@pytest.mark.parametrize(
    "state,detail", [("bogus", "invalid roster state: bogus"), ("dropped", "enrollment not found")]
)
def test_roster_state_owner_errors_keep_inline_detail_and_retarget(roster_state_route, monkeypatch, state, detail):
    from unittest.mock import Mock

    import dotmac_learning as learning
    from fastapi.responses import HTMLResponse

    case = roster_state_route
    _state_target(case, None)
    owner = Mock(side_effect=learning.ParticipationNotFound(detail))
    render = Mock(return_value=HTMLResponse(detail))
    monkeypatch.setattr(learning, "set_roster_state", owner)
    monkeypatch.setattr(case.route.templates, "TemplateResponse", render)
    request = _removal_request(case, htmx=True)
    response = case.route.change_roster_state(
        case.cohort_id,
        case.person_id,
        request,
        state=state,
        return_to="",
        person=case.actor,
        db=case.db,
    )
    render.assert_called_once_with(request, "instructor/_form_error.html", {"request": request, "message": detail})
    assert response.status_code == 200
    assert response.headers["HX-Retarget"] == f"#roster-state-error-{case.cohort_id}-{case.person_id}"
    assert response.headers["HX-Reswap"] == "innerHTML"
    case.admin.assert_not_called()


def test_roster_state_required_owner_read_failure_is_not_an_inline_not_found(roster_state_route, monkeypatch):
    from unittest.mock import Mock

    import dotmac_learning as learning

    from app.services.audit_context import HumanOperationError

    case = roster_state_route
    _state_target(case, "student")
    failure = RuntimeError("required owner read failure")
    monkeypatch.setattr(learning, "set_roster_state", Mock(side_effect=failure))
    error_renderer = Mock()
    monkeypatch.setattr(case.route, "_hx_error", error_renderer)
    with pytest.raises(HumanOperationError) as caught:
        case.route.change_roster_state(
            case.cohort_id,
            case.person_id,
            _removal_request(case),
            state="bogus",
            return_to="",
            person=case.actor,
            db=case.db,
        )
    assert caught.value.__context__ is failure
    error_renderer.assert_not_called()
    case.admin.assert_not_called()
    case.db.commit.assert_not_called()
    case.db.rollback.assert_not_called()


def test_roster_state_transaction_changed_refusal_propagates_to_caller_boundary(roster_state_route, monkeypatch):
    from unittest.mock import Mock

    import dotmac_learning as learning

    case = roster_state_route
    _state_target(case, "student")
    failure = learning.TransactionChanged("target has pending caller changes")
    monkeypatch.setattr(learning, "set_roster_state", Mock(side_effect=failure))
    error_renderer, redirect = Mock(), Mock()
    monkeypatch.setattr(case.route, "_hx_error", error_renderer)
    monkeypatch.setattr(case.route, "hx_redirect", redirect)
    with pytest.raises(learning.TransactionChanged) as caught:
        case.route.change_roster_state(
            case.cohort_id,
            case.person_id,
            _removal_request(case),
            state="dropped",
            return_to="",
            person=case.actor,
            db=case.db,
        )
    assert caught.value is failure
    error_renderer.assert_not_called()
    redirect.assert_not_called()
    case.admin.assert_not_called()
    case.db.commit.assert_not_called()
    case.db.rollback.assert_not_called()
