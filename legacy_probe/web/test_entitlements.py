"""Slice 1 — course entitlement enforcement (findings #1, #2).

A student may only open/submit a course's activities when an active Enrollment
links them to a Cohort that has an active CourseOffering for that course.
Discipline-string matching no longer grants access.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.models.assessment import Activity, Question, QuestionBank, Submission
from app.models.cohort import Cohort, Enrollment
from app.models.course import Chapter, Course
from app.models.offering import CourseOffering
from app.services.security import hash_password
from legacy_probe.conftest import grant_role, make_person_credential, make_person_party, web_login

H = {"Host": "alpha.localhost"}

def _login(app_client, admin_session, tenant, email="stu@a.edu"):
    p = make_person_party(admin_session, tenant_id=tenant.id, email=email, first_name="Stu", last_name="Dent")
    admin_session.add(p)
    admin_session.flush()
    admin_session.add(
        make_person_credential(admin_session, tenant_id=tenant.id, party_id=p.id, email=email, password_hash=hash_password("password1"))
    )
    grant_role(admin_session, tenant_id=tenant.id, party_id=p.id)
    admin_session.commit()
    web_login(app_client, H, email, "password1")
    return p

def _seed_course(admin_session, tenant, slug, *, discipline="networking", status="published"):
    """Course + chapter 1 + bank + one question + an mcq activity. Returns (course, activity)."""
    c = Course(tenant_id=tenant.id, slug=slug, title=slug.title(),
               discipline=discipline, source_ref="x", version=1, status=status, listed=False)
    admin_session.add(c)
    admin_session.flush()
    admin_session.add(Chapter(tenant_id=tenant.id, course_id=c.id, number=1, title="One",
                              part="I", body_html="<p>b</p>", source_hash="h", order_index=1))
    bank = QuestionBank(tenant_id=tenant.id, course_id=c.id, chapter_number=1,
                        kind="chapter", version=1)
    admin_session.add(bank)
    admin_session.flush()
    admin_session.add(Question(tenant_id=tenant.id, bank_id=bank.id, ext_id="q1", stem="Pick A",
                               type="single", options=["A", "B"], correct=["A"],
                               rubric_category="recall", explanation="Because A", weight=1))
    act = Activity(tenant_id=tenant.id, course_id=c.id, chapter_number=1, type="mcq_test",
                   bank_id=bank.id, title=f"{slug} Ch1", pass_threshold=0.6, grading="auto", weight=1.0)
    admin_session.add(act)
    admin_session.flush()
    return c, act

def _enroll(admin_session, tenant, person, *, discipline="networking"):
    coh = Cohort(tenant_id=tenant.id, name="Cohort", discipline=discipline, status="active")
    admin_session.add(coh)
    admin_session.flush()
    admin_session.add(Enrollment(tenant_id=tenant.id, cohort_id=coh.id, person_id=person.id,
                                 role_in_cohort="student", status="active"))
    admin_session.flush()
    return coh

def _offer(admin_session, tenant, cohort, course, *, starts_at=None, ends_at=None):
    admin_session.add(CourseOffering(tenant_id=tenant.id, cohort_id=cohort.id,
                                     course_id=course.id, status="active",
                                     starts_at=starts_at, ends_at=ends_at))
    admin_session.flush()

def _cleanup(admin_session, tenant):
    admin_session.query(Course).filter(Course.tenant_id == tenant.id).delete()
    admin_session.query(Cohort).filter(Cohort.tenant_id == tenant.id).delete()
    admin_session.commit()

def test_enrolled_student_can_open_and_submit(app_client, admin_session, tenant_a):
    p = _login(app_client, admin_session, tenant_a)
    course, act = _seed_course(admin_session, tenant_a, "alpha-course")
    coh = _enroll(admin_session, tenant_a, p)
    _offer(admin_session, tenant_a, coh, course)
    admin_session.commit()
    try:
        assert app_client.get("/courses/alpha-course/chapters/1", headers=H).status_code == 200
        assert app_client.get(f"/activities/{act.id}", headers=H).status_code == 200
        csrf = app_client.cookies.get("csrf_token", "")
        r = app_client.post(f"/activities/{act.id}/submit",
                            headers={**H, "x-csrf-token": csrf}, data={"q1": "A"})
        assert r.status_code == 200
        assert "Passed" in r.text
    finally:
        _cleanup(admin_session, tenant_a)

def test_unenrolled_student_forbidden_on_read(app_client, admin_session, tenant_a):
    """Finding #1: no offering for this course → 403 on chapter and activity GET."""
    _login(app_client, admin_session, tenant_a)
    course, act = _seed_course(admin_session, tenant_a, "secret-course")
    admin_session.commit()  # no enrollment, no offering
    try:
        assert app_client.get("/courses/secret-course/chapters/1", headers=H).status_code == 403
        assert app_client.get(f"/activities/{act.id}", headers=H).status_code == 403
    finally:
        _cleanup(admin_session, tenant_a)

def test_unenrolled_student_cannot_submit(app_client, admin_session, tenant_a):
    """Finding #1: submit to a course with no offering → 403 and no Submission written."""
    p = _login(app_client, admin_session, tenant_a)
    ok_course, _ = _seed_course(admin_session, tenant_a, "ok-course")
    secret, secret_act = _seed_course(admin_session, tenant_a, "secret2")
    coh = _enroll(admin_session, tenant_a, p)
    _offer(admin_session, tenant_a, coh, ok_course)  # access to ok-course only
    admin_session.commit()
    try:
        # GET an accessible chapter to obtain the csrf cookie.
        app_client.get("/courses/ok-course/chapters/1", headers=H)
        csrf = app_client.cookies.get("csrf_token", "")
        r = app_client.post(f"/activities/{secret_act.id}/submit",
                            headers={**H, "x-csrf-token": csrf}, data={"q1": "A"})
        assert r.status_code == 403
        n = admin_session.scalar(
            select(func.count()).select_from(Submission)
            .where(Submission.tenant_id == tenant_a.id)
            .where(Submission.activity_id == secret_act.id)
        )
        assert n == 0
    finally:
        _cleanup(admin_session, tenant_a)

def test_same_discipline_without_offering_has_no_access(app_client, admin_session, tenant_a):
    """Finding #2: sharing the cohort's discipline no longer grants access without an offering."""
    p = _login(app_client, admin_session, tenant_a)
    course, act = _seed_course(admin_session, tenant_a, "disc-course", discipline="networking")
    _enroll(admin_session, tenant_a, p, discipline="networking")  # same discipline, NO offering
    admin_session.commit()
    try:
        assert app_client.get(f"/activities/{act.id}", headers=H).status_code == 403
        # And the course must not appear on the dashboard.
        body = app_client.get("/", headers=H).text
        assert "Disc-Course" not in body
    finally:
        _cleanup(admin_session, tenant_a)

def test_future_offering_blocks_activity(app_client, admin_session, tenant_a):
    """Slice 2a: entitled but the offering opens in the future → 403."""
    p = _login(app_client, admin_session, tenant_a)
    course, act = _seed_course(admin_session, tenant_a, "future-course")
    coh = _enroll(admin_session, tenant_a, p)
    _offer(admin_session, tenant_a, coh, course,
           starts_at=datetime.now(UTC) + timedelta(days=1))
    admin_session.commit()
    try:
        assert app_client.get(f"/activities/{act.id}", headers=H).status_code == 403
        assert app_client.get("/courses/future-course/chapters/1", headers=H).status_code == 403
    finally:
        _cleanup(admin_session, tenant_a)

def test_past_offering_blocks_activity(app_client, admin_session, tenant_a):
    """Slice 2a: the offering window has closed → 403."""
    p = _login(app_client, admin_session, tenant_a)
    course, act = _seed_course(admin_session, tenant_a, "past-course")
    coh = _enroll(admin_session, tenant_a, p)
    _offer(admin_session, tenant_a, coh, course,
           ends_at=datetime.now(UTC) - timedelta(days=1))
    admin_session.commit()
    try:
        assert app_client.get(f"/activities/{act.id}", headers=H).status_code == 403
    finally:
        _cleanup(admin_session, tenant_a)

def test_enrollment_access_extension_reopens_expired_offering_for_that_learner(app_client, admin_session, tenant_a):
    p = _login(app_client, admin_session, tenant_a, email="extended@a.edu")
    other = _login(app_client, admin_session, tenant_a, email="not-extended@a.edu")
    course, act = _seed_course(admin_session, tenant_a, "extended-course")
    coh = _enroll(admin_session, tenant_a, p)
    admin_session.add(
        Enrollment(
            tenant_id=tenant_a.id,
            cohort_id=coh.id,
            person_id=other.id,
            role_in_cohort="student",
            status="active",
        )
    )
    enrollment = admin_session.scalars(
        select(Enrollment).where(Enrollment.person_id == p.id).where(Enrollment.cohort_id == coh.id)
    ).one()
    enrollment.access_ends_at = datetime.now(UTC) + timedelta(days=7)
    _offer(admin_session, tenant_a, coh, course, ends_at=datetime.now(UTC) - timedelta(days=1))
    admin_session.commit()
    try:
        app_client.cookies.clear()
        web_login(app_client, H, "extended@a.edu", "password1")
        assert app_client.get(f"/activities/{act.id}", headers=H).status_code == 200
        app_client.cookies.clear()
        web_login(app_client, H, "not-extended@a.edu", "password1")
        assert app_client.get(f"/activities/{act.id}", headers=H).status_code == 403
    finally:
        _cleanup(admin_session, tenant_a)

def test_open_window_allows_activity(app_client, admin_session, tenant_a):
    """Slice 2a: now within [starts_at, ends_at] → 200."""
    p = _login(app_client, admin_session, tenant_a)
    course, act = _seed_course(admin_session, tenant_a, "open-course")
    coh = _enroll(admin_session, tenant_a, p)
    _offer(admin_session, tenant_a, coh, course,
           starts_at=datetime.now(UTC) - timedelta(days=1),
           ends_at=datetime.now(UTC) + timedelta(days=1))
    admin_session.commit()
    try:
        assert app_client.get(f"/activities/{act.id}", headers=H).status_code == 200
    finally:
        _cleanup(admin_session, tenant_a)

def test_draft_course_hidden_from_learner(app_client, admin_session, tenant_a):
    """Slice 5/#8: a draft course, even offered+entitled, is not accessible."""
    p = _login(app_client, admin_session, tenant_a)
    course, act = _seed_course(admin_session, tenant_a, "draft-course", status="draft")
    coh = _enroll(admin_session, tenant_a, p)
    _offer(admin_session, tenant_a, coh, course)
    admin_session.commit()
    try:
        assert app_client.get(f"/activities/{act.id}", headers=H).status_code == 403
        assert app_client.get("/courses/draft-course/chapters/1", headers=H).status_code == 403
        assert "Draft-Course" not in app_client.get("/", headers=H).text
        # Publishing makes it accessible.
        course.status = "published"
        admin_session.commit()
        assert app_client.get(f"/activities/{act.id}", headers=H).status_code == 200
    finally:
        _cleanup(admin_session, tenant_a)

def test_dashboard_lists_only_offered_courses(app_client, admin_session, tenant_a):
    p = _login(app_client, admin_session, tenant_a)
    offered, _ = _seed_course(admin_session, tenant_a, "offered")
    _seed_course(admin_session, tenant_a, "not-offered")
    coh = _enroll(admin_session, tenant_a, p)
    _offer(admin_session, tenant_a, coh, offered)
    admin_session.commit()
    try:
        body = app_client.get("/", headers=H).text
        assert "Offered" in body
        assert "Not-Offered" not in body
    finally:
        _cleanup(admin_session, tenant_a)
