# tests/web/test_catalog.py
"""Web tests for the course catalog — GET /courses and GET /courses/{slug}."""

from __future__ import annotations

from dotmac_kernel.models import PartyRoleGrant

from app.models.assessment import Activity, Question, QuestionBank
from app.models.cohort import Cohort, Enrollment
from app.models.course import Chapter, Course
from app.models.offering import CourseOffering
from app.models.prerequisite import CoursePrerequisite
from app.services.bootstrap import ensure_roles
from app.services.security import hash_password
from legacy_probe.conftest import grant_role, make_person_credential, make_person_party, web_login

H = {"Host": "alpha.localhost"}

def _login(app_client, admin_session, tenant, email="cat_web_stu@a.edu"):
    p = make_person_party(admin_session, tenant_id=tenant.id, email=email, first_name="Cat", last_name="Web")
    admin_session.add(p)
    admin_session.flush()
    admin_session.add(
        make_person_credential(admin_session, tenant_id=tenant.id, party_id=p.id, email=email, password_hash=hash_password("password1"))
    )
    grant_role(admin_session, tenant_id=tenant.id, party_id=p.id, role="student")
    admin_session.commit()
    web_login(app_client, H, email, "password1")
    return p

def _login_staff(app_client, admin_session, tenant, role="instructor", email="cat_web_ins@a.edu"):
    roles = ensure_roles(admin_session, tenant.id)
    p = make_person_party(admin_session, tenant_id=tenant.id, email=email, first_name="Staff", last_name="User")
    admin_session.add(p)
    admin_session.flush()
    admin_session.add(
        make_person_credential(admin_session, tenant_id=tenant.id, party_id=p.id, email=email, password_hash=hash_password("password1"))
    )
    admin_session.add(PartyRoleGrant(tenant_id=tenant.id, party_id=p.id, role_id=roles[role].id))
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
    admin_session.add(
        Chapter(tenant_id=tenant.id, course_id=c.id, number=1, title="Ch1",
                part="I", body_html="<p>x</p>", source_hash="h", order_index=1)
    )
    bank = QuestionBank(tenant_id=tenant.id, course_id=c.id, chapter_number=1, kind="chapter", version=1)
    admin_session.add(bank)
    admin_session.flush()
    admin_session.add(
        Question(tenant_id=tenant.id, bank_id=bank.id, ext_id="q1", stem="Q?", type="single",
                 options=["A", "B"], correct=["A"], rubric_category="recall", explanation="", weight=1)
    )
    admin_session.add(
        Activity(tenant_id=tenant.id, course_id=c.id, chapter_number=1,
                 type="mcq_test", bank_id=bank.id, title=f"{title} Act", pass_threshold=0.6, grading="auto", weight=1.0)
    )
    admin_session.flush()
    return c

def _enroll(admin_session, tenant, person, course):
    coh = Cohort(tenant_id=tenant.id, name="Coh", discipline="networking", status="active")
    admin_session.add(coh)
    admin_session.flush()
    admin_session.add(
        Enrollment(tenant_id=tenant.id, cohort_id=coh.id, person_id=person.id,
                   role_in_cohort="student", status="active")
    )
    admin_session.add(
        CourseOffering(tenant_id=tenant.id, cohort_id=coh.id, course_id=course.id, status="active")
    )
    admin_session.commit()

def _cleanup(admin_session, tenant):
    admin_session.query(Course).filter(Course.tenant_id == tenant.id).delete()
    admin_session.query(Cohort).filter(Cohort.tenant_id == tenant.id).delete()
    admin_session.commit()

# ── /courses list ──────────────────────────────────────────────────────────────

def test_courses_list_shows_my_courses(app_client, admin_session, tenant_a):
    """Enrolled student sees their course in /courses."""
    p = _login(app_client, admin_session, tenant_a, "cat_list1@a.edu")
    c = _seed_course(admin_session, tenant_a, "cat-list-c1", "List Course One")
    _enroll(admin_session, tenant_a, p, c)
    try:
        r = app_client.get("/courses", headers=H)
        assert r.status_code == 200
        assert "List Course One" in r.text
    finally:
        _cleanup(admin_session, tenant_a)

def test_courses_list_excludes_non_enrolled(app_client, admin_session, tenant_a):
    """Non-enrolled courses do not appear in My courses."""
    _login(app_client, admin_session, tenant_a, "cat_list2@a.edu")
    _seed_course(admin_session, tenant_a, "cat-list-noe", "Not Enrolled Course")
    admin_session.commit()
    try:
        r = app_client.get("/courses", headers=H)
        assert r.status_code == 200
        assert "Not Enrolled Course" not in r.text
    finally:
        _cleanup(admin_session, tenant_a)

def test_courses_list_staff_sees_all_courses(app_client, admin_session, tenant_a):
    """Instructor/admin sees both 'My courses' and 'All courses' section."""
    _login_staff(app_client, admin_session, tenant_a, role="instructor", email="cat_list_ins@a.edu")
    _seed_course(admin_session, tenant_a, "cat-list-all", "All Courses Entry")
    admin_session.commit()
    try:
        r = app_client.get("/courses", headers=H)
        assert r.status_code == 200
        # Staff sees an "All courses" section containing the course.
        assert "All courses" in r.text
        assert "All Courses Entry" in r.text
    finally:
        _cleanup(admin_session, tenant_a)

def test_enrolled_instructor_sees_course_in_learning_workspace(
    app_client, admin_session, tenant_a
):
    """A teaching role must not hide the person's own learner enrollment."""
    person = _login_staff(
        app_client,
        admin_session,
        tenant_a,
        role="instructor",
        email="dual_mode_instructor@a.edu",
    )
    course = _seed_course(
        admin_session,
        tenant_a,
        "instructor-learning-course",
        "Instructor Learning Course",
    )
    _enroll(admin_session, tenant_a, person, course)
    try:
        response = app_client.get("/learn", headers=H)
        assert response.status_code == 200
        assert "Instructor Learning Course" in response.text
        assert 'aria-label="Switch to Learning workspace" aria-current="page"' in response.text
        assert 'aria-label="Switch to Teaching workspace"' in response.text
    finally:
        _cleanup(admin_session, tenant_a)

def test_courses_list_student_no_all_courses_section(app_client, admin_session, tenant_a):
    """Student does not see the 'All courses' section."""
    _login(app_client, admin_session, tenant_a, "cat_list_noall@a.edu")
    _seed_course(admin_session, tenant_a, "cat-list-noall", "Hidden All")
    admin_session.commit()
    try:
        r = app_client.get("/courses", headers=H)
        assert r.status_code == 200
        assert "All courses" not in r.text
    finally:
        _cleanup(admin_session, tenant_a)

# ── /courses/{slug} landing ────────────────────────────────────────────────────

def test_course_landing_enrolled_student_200(app_client, admin_session, tenant_a):
    """Enrolled student can view the course landing page."""
    p = _login(app_client, admin_session, tenant_a, "cat_land1@a.edu")
    c = _seed_course(admin_session, tenant_a, "cat-land1", "Landing Course")
    _enroll(admin_session, tenant_a, p, c)
    try:
        r = app_client.get(f"/courses/{c.slug}", headers=H)
        assert r.status_code == 200
        assert "Landing Course" in r.text
    finally:
        _cleanup(admin_session, tenant_a)

def test_course_landing_unknown_slug_404(app_client, admin_session, tenant_a):
    """Unknown course slug returns 404."""
    _login(app_client, admin_session, tenant_a, "cat_land_404@a.edu")
    admin_session.commit()
    r = app_client.get("/courses/no-such-slug", headers=H)
    assert r.status_code == 404

def test_course_landing_non_enrolled_student_403(app_client, admin_session, tenant_a):
    """Non-enrolled student gets 403 on course landing."""
    _login(app_client, admin_session, tenant_a, "cat_land403@a.edu")
    c = _seed_course(admin_session, tenant_a, "cat-land403", "Forbidden Course")
    admin_session.commit()
    try:
        r = app_client.get(f"/courses/{c.slug}", headers=H)
        assert r.status_code == 403
    finally:
        _cleanup(admin_session, tenant_a)

def test_course_landing_staff_bypass_no_enrollment(app_client, admin_session, tenant_a):
    """Instructor can view course landing even without enrollment."""
    _login_staff(app_client, admin_session, tenant_a, role="instructor", email="cat_land_ins@a.edu")
    c = _seed_course(admin_session, tenant_a, "cat-land-staff", "Staff View Course")
    admin_session.commit()
    try:
        r = app_client.get(f"/courses/{c.slug}", headers=H)
        assert r.status_code == 200
        assert "Staff View Course" in r.text
    finally:
        _cleanup(admin_session, tenant_a)

def test_course_landing_shows_parts(app_client, admin_session, tenant_a):
    """Course landing renders Part labels from Chapter.part."""
    p = _login(app_client, admin_session, tenant_a, "cat_parts@a.edu")
    c = _seed_course(admin_session, tenant_a, "cat-parts", "Parts Course")
    # Override chapter to have a distinct part label
    admin_session.query(Chapter).filter(
        Chapter.tenant_id == tenant_a.id, Chapter.course_id == c.id
    ).update({"part": "II"})
    _enroll(admin_session, tenant_a, p, c)
    try:
        r = app_client.get(f"/courses/{c.slug}", headers=H)
        assert r.status_code == 200
        assert "Part II" in r.text
    finally:
        _cleanup(admin_session, tenant_a)

def test_course_landing_locked_banner_when_prereq_unmet(app_client, admin_session, tenant_a):
    """Enrolled student with unmet prerequisites sees the locked banner."""
    p = _login(app_client, admin_session, tenant_a, "cat_prereq@a.edu")
    prereq = _seed_course(admin_session, tenant_a, "cat-pr-prereq", "Prereq Course")
    main_c = _seed_course(admin_session, tenant_a, "cat-pr-main", "Main Locked")
    admin_session.add(
        CoursePrerequisite(tenant_id=tenant_a.id, course_id=main_c.id, requires_course_id=prereq.id)
    )
    # Enroll in main course (but not prereq completed)
    coh = Cohort(tenant_id=tenant_a.id, name="Coh", discipline="networking", status="active")
    admin_session.add(coh)
    admin_session.flush()
    admin_session.add(
        Enrollment(tenant_id=tenant_a.id, cohort_id=coh.id, person_id=p.id,
                   role_in_cohort="student", status="active")
    )
    admin_session.add(
        CourseOffering(tenant_id=tenant_a.id, cohort_id=coh.id, course_id=main_c.id, status="active")
    )
    admin_session.commit()
    try:
        r = app_client.get(f"/courses/{main_c.slug}", headers=H)
        assert r.status_code == 200
        # The locked banner text
        assert "Prerequisite" in r.text
    finally:
        _cleanup(admin_session, tenant_a)

def test_courses_nav_item_present(app_client, admin_session, tenant_a):
    """The learn sidebar contains a Courses nav link."""
    _login(app_client, admin_session, tenant_a, "cat_nav@a.edu")
    admin_session.commit()
    r = app_client.get("/", headers=H)
    assert r.status_code == 200
    assert "/courses" in r.text
    assert "Courses" in r.text
