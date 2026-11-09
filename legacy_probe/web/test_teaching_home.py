"""Tests for the Teaching Home web route (GET /instructor)."""

from __future__ import annotations

from legacy_probe.web.test_reports import _login, _seed_cohort, _seed_login


def test_instructor_sees_teaching_home(app_client, admin_session, tenant_a):
    _seed_login(admin_session, tenant_a, "inst@a.edu", "instructor")
    cohort, _ = _seed_cohort(admin_session, tenant_a.id)
    h = _login(app_client, "inst@a.edu")
    r = app_client.get("/instructor", headers=h)
    assert r.status_code == 200
    # My cohorts lists the seeded cohort name + count.
    assert "Abuja 2026" in r.text
    # Quick links present.
    assert f"/instructor/cohorts/{cohort.id}" in r.text
    assert "/reports" in r.text


def test_admin_allowed_on_teaching_home(app_client, admin_session, tenant_a):
    _seed_login(admin_session, tenant_a, "admin@a.edu", "admin")
    h = _login(app_client, "admin@a.edu")
    r = app_client.get("/instructor", headers=h)
    assert r.status_code == 200


def test_student_forbidden_on_teaching_home(app_client, admin_session, tenant_a):
    _seed_login(admin_session, tenant_a, "stud@a.edu", "student")
    h = _login(app_client, "stud@a.edu")
    assert app_client.get("/instructor", headers=h).status_code == 403


def test_instructor_cohorts_still_works(app_client, admin_session, tenant_a):
    """Regression: the new GET /instructor must not shadow /instructor/cohorts."""
    _seed_login(admin_session, tenant_a, "inst@a.edu", "instructor")
    _seed_cohort(admin_session, tenant_a.id)
    h = _login(app_client, "inst@a.edu")
    r = app_client.get("/instructor/cohorts", headers=h)
    assert r.status_code == 200
