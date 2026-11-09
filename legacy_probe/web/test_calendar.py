# tests/web/test_calendar.py
"""Web tests for GET /calendar."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models.assessment import Activity, Question, QuestionBank
from app.models.cohort import Cohort, Enrollment
from app.models.course import Course
from app.models.offering import CourseOffering
from app.models.pacing import OfferingActivity
from app.services.security import hash_password
from legacy_probe.conftest import grant_role, make_person_credential, make_person_party, web_login

H = {"Host": "alpha.localhost"}

def _login(app_client, admin_session, tenant, email="cal_stu@a.edu"):
    p = make_person_party(admin_session, tenant_id=tenant.id, email=email, first_name="Cal", last_name="Stu")
    admin_session.add(p)
    admin_session.flush()
    admin_session.add(
        make_person_credential(admin_session, tenant_id=tenant.id, party_id=p.id, email=email, password_hash=hash_password("password1"))
    )
    grant_role(admin_session, tenant_id=tenant.id, party_id=p.id)
    admin_session.commit()
    web_login(app_client, H, email, "password1")
    return p

def _seed_course(admin_session, tenant, slug, title="Course"):
    c = Course(
        tenant_id=tenant.id, slug=slug, title=title, discipline="networking",
        source_ref="x", version=1, status="published",
        listed=False,
    )
    admin_session.add(c)
    admin_session.flush()
    return c

def _seed_activity(admin_session, tenant, course_id, title="Act"):
    bank = QuestionBank(tenant_id=tenant.id, course_id=course_id, chapter_number=1, kind="chapter", version=1)
    admin_session.add(bank)
    admin_session.flush()
    admin_session.add(Question(
        tenant_id=tenant.id, bank_id=bank.id, ext_id="q1", stem="Q?", type="single",
        options=["A", "B"], correct=["A"], rubric_category="recall", explanation="", weight=1,
    ))
    act = Activity(
        tenant_id=tenant.id, course_id=course_id, chapter_number=1,
        type="mcq_test", bank_id=bank.id, title=title, pass_threshold=0.6, grading="auto", weight=1.0,
    )
    admin_session.add(act)
    admin_session.flush()
    return act

def _enroll_with_offering(admin_session, tenant, person_id, course_id):
    coh = Cohort(tenant_id=tenant.id, name="Coh", discipline="networking", status="active")
    admin_session.add(coh)
    admin_session.flush()
    admin_session.add(
        Enrollment(tenant_id=tenant.id, cohort_id=coh.id, person_id=person_id,
                   role_in_cohort="student", status="active")
    )
    off = CourseOffering(
        tenant_id=tenant.id, cohort_id=coh.id, course_id=course_id, status="active",
    )
    admin_session.add(off)
    admin_session.flush()
    return off

def _cleanup(admin_session, tenant):
    admin_session.query(Course).filter(Course.tenant_id == tenant.id).delete()
    admin_session.query(Cohort).filter(Cohort.tenant_id == tenant.id).delete()
    admin_session.commit()

def test_calendar_requires_auth(app_client, admin_session, tenant_a):
    """Unauthenticated request to /calendar is redirected to login."""
    admin_session.commit()  # ensure tenant exists
    r = app_client.get("/calendar", headers=H, follow_redirects=False)
    assert r.status_code in (302, 303, 307)

def test_calendar_empty_for_no_events(app_client, admin_session, tenant_a):
    """Authenticated user with no upcoming events sees empty state."""
    _login(app_client, admin_session, tenant_a, "cal_empty@a.edu")
    r = app_client.get("/calendar", headers=H)
    assert r.status_code == 200
    assert "No upcoming events" in r.text

def test_calendar_shows_future_due_at(app_client, admin_session, tenant_a):
    """A future activity due_at in an accessible course appears in /calendar."""
    person = _login(app_client, admin_session, tenant_a, "cal_due@a.edu")
    course = _seed_course(admin_session, tenant_a, "cal-due-c", "Due Course")
    act = _seed_activity(admin_session, tenant_a, course.id, "Important Quiz")
    off = _enroll_with_offering(admin_session, tenant_a, person.id, course.id)

    future = datetime.now(UTC) + timedelta(days=3)
    admin_session.add(OfferingActivity(
        tenant_id=tenant_a.id, offering_id=off.id, activity_id=act.id, due_at=future,
    ))
    admin_session.commit()
    try:
        r = app_client.get("/calendar", headers=H)
        assert r.status_code == 200
        assert "Important Quiz" in r.text
        assert "Due Course" in r.text
    finally:
        _cleanup(admin_session, tenant_a)

def test_calendar_excludes_non_accessible(app_client, admin_session, tenant_a):
    """Activity due_at for a course the person is NOT enrolled in is excluded."""
    person = _login(app_client, admin_session, tenant_a, "cal_noac@a.edu")
    other = make_person_party(admin_session, tenant_id=tenant_a.id, email="cal_noac2@a.edu", first_name="O", last_name="T")
    admin_session.add(other)
    admin_session.flush()

    acc_course = _seed_course(admin_session, tenant_a, "cal-acc", "My Course")
    inacc_course = _seed_course(admin_session, tenant_a, "cal-noacc", "Secret Course")
    inacc_act = _seed_activity(admin_session, tenant_a, inacc_course.id, "Secret Quiz")

    _enroll_with_offering(admin_session, tenant_a, person.id, acc_course.id)
    off_inacc = _enroll_with_offering(admin_session, tenant_a, other.id, inacc_course.id)

    future = datetime.now(UTC) + timedelta(days=2)
    admin_session.add(OfferingActivity(
        tenant_id=tenant_a.id, offering_id=off_inacc.id, activity_id=inacc_act.id, due_at=future,
    ))
    admin_session.commit()
    try:
        r = app_client.get("/calendar", headers=H)
        assert r.status_code == 200
        assert "Secret Quiz" not in r.text
    finally:
        _cleanup(admin_session, tenant_a)

def test_calendar_nav_item_present(app_client, admin_session, tenant_a):
    """The learn sidebar contains a Calendar nav link."""
    _login(app_client, admin_session, tenant_a, "cal_nav@a.edu")
    admin_session.commit()
    r = app_client.get("/", headers=H)
    assert r.status_code == 200
    assert "/calendar" in r.text
    assert "Calendar" in r.text
