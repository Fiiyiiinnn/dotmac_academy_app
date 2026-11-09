"""Tests for the instructor/admin Reports web routes."""

from __future__ import annotations

from app.models.assessment import Activity, Score, Submission
from app.models.cohort import Cohort, Enrollment
from app.models.course import Course
from app.models.offering import CourseOffering
from legacy_probe.owner_helpers import person as _person
from legacy_probe.owner_helpers import seed_login_with_account as _seed_login
from legacy_probe.web.test_reports_access_policy import _restore_settings

assert _restore_settings


def _seed_cohort(admin_session, tid):
    c = Course(tenant_id=tid, slug="net", title="Networking", discipline="networking",
               source_ref="x", version=1, status="published", listed=False)
    admin_session.add(c)
    admin_session.flush()
    a1 = Activity(tenant_id=tid, course_id=c.id, chapter_number=1, type="mcq_test",
                  title="Ch1 Test", pass_threshold=0.6, grading="auto", weight=1.0)
    a2 = Activity(tenant_id=tid, course_id=c.id, chapter_number=2, type="lab",
                  title="Ch2 Lab", pass_threshold=0.6, grading="auto", weight=1.0)
    admin_session.add_all([a1, a2])
    admin_session.flush()
    coh = Cohort(tenant_id=tid, name="Abuja 2026", discipline="networking", status="active")
    admin_session.add(coh)
    admin_session.flush()
    admin_session.add(CourseOffering(tenant_id=tid, cohort_id=coh.id, course_id=c.id, status="active"))
    stu_a = _person(admin_session, tid, email="a@stu.edu", first_name="Aaa", last_name="Student")
    stu_b = _person(admin_session, tid, email="b@stu.edu", first_name="Bbb", last_name="Student")
    admin_session.add_all([stu_a, stu_b])
    admin_session.flush()
    for p in (stu_a, stu_b):
        admin_session.add(Enrollment(tenant_id=tid, cohort_id=coh.id, person_id=p.id,
                                     role_in_cohort="student", status="active"))
    admin_session.flush()
    sub = Submission(tenant_id=tid, activity_id=a1.id, person_id=stu_a.id, answers={}, attempt_no=1)
    admin_session.add(sub)
    admin_session.flush()
    admin_session.add(Score(tenant_id=tid, submission_id=sub.id, score=10, max_score=10,
                            fraction=1.0, passed=True, per_item=[], source="auto"))
    admin_session.commit()
    return coh, stu_a


def _login(app_client, email):
    return {"Host": "alpha.localhost"}


def test_student_forbidden_on_reports(app_client, admin_session, tenant_a, current_account):
    _seed_login(app_client, admin_session, tenant_a, "stud@a.edu", "student", current_account)
    h = _login(app_client, "stud@a.edu")
    assert app_client.get("/instructor/reports", headers=h).status_code == 403


def test_instructor_reports_index(app_client, admin_session, tenant_a, current_account):
    _seed_login(app_client, admin_session, tenant_a, "inst@a.edu", "instructor", current_account)
    coh, _ = _seed_cohort(admin_session, tenant_a.id)
    h = _login(app_client, "inst@a.edu")
    r = app_client.get("/instructor/reports", headers=h)
    assert r.status_code == 200
    assert "Abuja 2026" in r.text


def test_instructor_can_view_cohort_matrix(app_client, admin_session, tenant_a, current_account):
    _seed_login(app_client, admin_session, tenant_a, "inst@a.edu", "instructor", current_account)
    coh, _ = _seed_cohort(admin_session, tenant_a.id)
    h = _login(app_client, "inst@a.edu")
    r = app_client.get(f"/instructor/reports/cohort/{coh.id}", headers=h)
    assert r.status_code == 200
    assert "a@stu.edu" in r.text
    assert "Ch1 Test" in r.text


def test_admin_can_view_cohort_matrix(app_client, admin_session, tenant_a, current_account):
    _seed_login(app_client, admin_session, tenant_a, "admin@a.edu", "admin", current_account)
    coh, _ = _seed_cohort(admin_session, tenant_a.id)
    h = _login(app_client, "admin@a.edu")
    r = app_client.get(f"/instructor/reports/cohort/{coh.id}", headers=h)
    assert r.status_code == 200


def test_student_transcript_view(app_client, admin_session, tenant_a, current_account):
    _seed_login(app_client, admin_session, tenant_a, "inst@a.edu", "instructor", current_account)
    coh, stu_a = _seed_cohort(admin_session, tenant_a.id)
    h = _login(app_client, "inst@a.edu")
    r = app_client.get(f"/instructor/reports/student/{stu_a.id}", headers=h)
    assert r.status_code == 200
    assert "Ch1 Test" in r.text


def test_cohort_csv_export(app_client, admin_session, tenant_a, current_account):
    _seed_login(app_client, admin_session, tenant_a, "inst@a.edu", "instructor", current_account)
    coh, _ = _seed_cohort(admin_session, tenant_a.id)
    h = _login(app_client, "inst@a.edu")
    r = app_client.get(f"/instructor/reports/cohort/{coh.id}.csv", headers=h)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert f"cohort-{coh.id}.csv" in r.headers.get("content-disposition", "")
    lines = [ln for ln in r.text.splitlines() if ln.strip()]
    # header + 2 student rows
    assert len(lines) == 3
    assert "a@stu.edu" in r.text and "b@stu.edu" in r.text


def test_cross_tenant_cohort_404(app_client, admin_session, tenant_a, tenant_b, current_account):
    _seed_login(app_client, admin_session, tenant_a, "inst@a.edu", "instructor", current_account)
    coh_b = Cohort(tenant_id=tenant_b.id, name="Beta", discipline="networking", status="active")
    admin_session.add(coh_b)
    admin_session.commit()
    admin_session.refresh(coh_b)
    h = _login(app_client, "inst@a.edu")
    assert app_client.get(f"/instructor/reports/cohort/{coh_b.id}", headers=h).status_code == 404


def test_analytics_csv_and_current_recipient_outbox(current_account, app_client):
    from dotmac_kernel.models import Party
    from sqlalchemy import select, update

    from app.models.email_outbox import EmailOutbox

    a = current_account
    h = _seed_login(app_client, a.admin, a.tenant, "reports-owner@a.edu", "instructor", a)
    cohort, student = _seed_cohort(a.admin, a.tid)
    assert app_client.get(f"/instructor/reports/cohort/{cohort.id}/analytics", headers=h).status_code == 200
    for suffix in ["roster.csv", "funnel.csv", "queue.csv"]:
        response = app_client.get(f"/instructor/reports/cohort/{cohort.id}/{suffix}", headers=h)
        assert response.status_code == 200 and response.headers["content-type"].startswith("text/csv")
    assert app_client.get("/instructor/reports/academy-summary.csv", headers=h).status_code == 200
    fresh_email = "fresh-reports-owner@a.edu"
    a.admin.execute(update(Party).where(Party.id == a.pid).values(email=fresh_email))
    a.admin.commit()
    headers = {**h, "x-csrf-token": app_client.cookies.get("csrf_token", "")}
    for path in [f"cohort/{cohort.id}", f"student/{student.id}"]:
        assert app_client.post(f"/instructor/reports/{path}/email", headers=headers).status_code == 200
    a.admin.expire_all()
    rows = a.admin.scalars(select(EmailOutbox).where(EmailOutbox.tenant_id == a.tid,
                             EmailOutbox.kind.in_(["cohort_report", "transcript"]))).all()
    assert {r.recipient for r in rows} == {fresh_email, student.email}
    assert all(r.status == "pending" for r in rows)
    assert a.login_snapshot.email != fresh_email


def test_reports_labels_use_configured_insight_windows(current_account, app_client):
    from app.services import settings_store
    a = current_account
    h = _seed_login(app_client, a.admin, a.tenant, "window-owner@a.edu", "instructor", a)
    cohort, student = _seed_cohort(a.admin, a.tid)
    settings_store.set_many(a.admin, {"learning_active_window_days": "5",
                                     "learning_comparison_prior_weeks": "2"})
    a.admin.commit()
    index = app_client.get("/instructor/reports", headers=h)
    assert index.status_code == 200 and "last 5 days" in index.text and "Active (5d)" in index.text
    analytics = app_client.get(f"/instructor/reports/cohort/{cohort.id}/analytics", headers=h)
    assert analytics.status_code == 200 and "Active / inactive (5d)" in analytics.text
    assert "2-week average" in analytics.text
