"""Tests for the instructor/admin Gradebook web routes."""
from __future__ import annotations

from app.models.assessment import Activity, Score, Submission
from app.models.cohort import Cohort, Enrollment
from app.models.course import Course
from app.models.offering import CourseOffering
from legacy_probe.conftest import make_canonical_account, web_login


def _seed_login(db, tenant, email, role_slug):
    p = make_canonical_account(
        db,
        tenant_id=tenant.id,
        email=email,
        first_name="Log",
        last_name="In",
        password="password1",
        role=role_slug,
    )
    db.commit()
    return p


def _seed_cohort(db, tid):
    """Cohort with two activities (weights 1 and 3) and two students; one score on a1."""
    course = Course(tenant_id=tid, slug="gb-net", title="GB Networking", discipline="networking",
                    source_ref="x", version=1, status="published", listed=False)
    db.add(course)
    db.flush()
    a1 = Activity(tenant_id=tid, course_id=course.id, chapter_number=1, type="mcq_test",
                  title="Quiz A", pass_threshold=0.6, weight=1.0, grading="auto")
    a2 = Activity(tenant_id=tid, course_id=course.id, chapter_number=2, type="mcq_test",
                  title="Quiz B", pass_threshold=0.6, weight=3.0, grading="auto")
    db.add_all([a1, a2])
    db.flush()
    coh = Cohort(tenant_id=tid, name="GB Abuja 2026", discipline="networking", status="active")
    db.add(coh)
    db.flush()
    db.add(CourseOffering(tenant_id=tid, cohort_id=coh.id, course_id=course.id, status="active"))
    stu_a = make_canonical_account(
        db, tenant_id=tid, email="gb_a@stu.edu", first_name="GbA", last_name="Student",
        password="password1", role="student",
    )
    stu_b = make_canonical_account(
        db, tenant_id=tid, email="gb_b@stu.edu", first_name="GbB", last_name="Student",
        password="password1", role="student",
    )
    for p in (stu_a, stu_b):
        db.add(Enrollment(tenant_id=tid, cohort_id=coh.id, person_id=p.id,
                          role_in_cohort="student", status="active"))
    db.flush()
    # stu_a scores 100% on a1
    sub = Submission(tenant_id=tid, activity_id=a1.id, person_id=stu_a.id, answers={}, attempt_no=1)
    db.add(sub)
    db.flush()
    db.add(Score(tenant_id=tid, submission_id=sub.id, score=10, max_score=10,
                 fraction=1.0, passed=True, per_item=[], source="auto"))
    db.commit()
    return coh, stu_a


def _login(client, email):
    h = {"Host": "alpha.localhost"}
    web_login(client, h, email, "password1")
    return h


def test_student_forbidden_on_gradebook(app_client, admin_session, tenant_a):
    _seed_login(admin_session, tenant_a, "stud@gb.edu", "student")
    h = _login(app_client, "stud@gb.edu")
    assert app_client.get("/instructor/gradebook", headers=h).status_code == 403


def test_student_forbidden_on_gradebook_cohort(app_client, admin_session, tenant_a):
    _seed_login(admin_session, tenant_a, "stud2@gb.edu", "student")
    coh, _ = _seed_cohort(admin_session, tenant_a.id)
    h = _login(app_client, "stud2@gb.edu")
    assert app_client.get(f"/instructor/gradebook/{coh.id}", headers=h).status_code == 403


def test_instructor_gradebook_index(app_client, admin_session, tenant_a):
    _seed_login(admin_session, tenant_a, "inst@gb.edu", "instructor")
    coh, _ = _seed_cohort(admin_session, tenant_a.id)
    h = _login(app_client, "inst@gb.edu")
    r = app_client.get("/instructor/gradebook", headers=h)
    assert r.status_code == 200
    assert "GB Abuja 2026" in r.text


def test_instructor_gradebook_cohort(app_client, admin_session, tenant_a):
    _seed_login(admin_session, tenant_a, "inst2@gb.edu", "instructor")
    coh, _ = _seed_cohort(admin_session, tenant_a.id)
    h = _login(app_client, "inst2@gb.edu")
    r = app_client.get(f"/instructor/gradebook/{coh.id}", headers=h)
    assert r.status_code == 200
    assert "gb_a@stu.edu" in r.text
    assert "Quiz A" in r.text
    # stu_a: a1=100%, a2=0% -> weighted (1x1.0 + 3x0.0)/(1+3) = 25%
    assert "25%" in r.text


def test_admin_gradebook_cohort(app_client, admin_session, tenant_a):
    _seed_login(admin_session, tenant_a, "admin@gb.edu", "admin")
    coh, _ = _seed_cohort(admin_session, tenant_a.id)
    h = _login(app_client, "admin@gb.edu")
    r = app_client.get(f"/instructor/gradebook/{coh.id}", headers=h)
    assert r.status_code == 200


def test_gradebook_csv_export(app_client, admin_session, tenant_a):
    _seed_login(admin_session, tenant_a, "inst3@gb.edu", "instructor")
    coh, _ = _seed_cohort(admin_session, tenant_a.id)
    h = _login(app_client, "inst3@gb.edu")
    r = app_client.get(f"/instructor/gradebook/{coh.id}.csv", headers=h)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert f"gradebook-{coh.id}.csv" in r.headers.get("content-disposition", "")
    lines = [ln for ln in r.text.splitlines() if ln.strip()]
    # header + 2 student rows
    assert len(lines) == 3
    assert "gb_a@stu.edu" in r.text
    assert "gb_b@stu.edu" in r.text
    # header has activity columns
    assert "Quiz A" in lines[0]
    assert "final_pct" in lines[0]


def test_cross_tenant_gradebook_404(app_client, admin_session, tenant_a, tenant_b):
    _seed_login(admin_session, tenant_a, "inst4@gb.edu", "instructor")
    coh_b = Cohort(tenant_id=tenant_b.id, name="Beta GB", discipline="networking", status="active")
    admin_session.add(coh_b)
    admin_session.commit()
    admin_session.refresh(coh_b)
    h = _login(app_client, "inst4@gb.edu")
    assert app_client.get(f"/instructor/gradebook/{coh_b.id}", headers=h).status_code == 404
