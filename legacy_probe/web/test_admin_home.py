"""Tests for the Admin Console web route (GET /admin)."""

from __future__ import annotations

from app.models.lab import LabInstance
from legacy_probe.web.test_reports import _login, _seed_cohort, _seed_login


def _seed_lab(admin_session, tid, status="active"):
    import uuid

    inst = LabInstance(
        tenant_id=tid,
        activity_id=uuid.uuid4(),
        person_id=uuid.uuid4(),
        # instance_name is now globally unique at the database — a fixed
        # literal collides the moment this helper is called more than once
        # (e.g. test_admin_sees_console calls it twice for two statuses).
        instance_name=f"lab-{uuid.uuid4()}",
        status=status,
    )
    admin_session.add(inst)
    admin_session.commit()
    return inst


def test_admin_sees_console(app_client, admin_session, tenant_a):
    _seed_login(admin_session, tenant_a, "admin@a.edu", "admin")
    _seed_cohort(admin_session, tenant_a.id)
    _seed_lab(admin_session, tenant_a.id, "active")
    _seed_lab(admin_session, tenant_a.id, "provisioning")
    h = _login(app_client, "admin@a.edu")
    r = app_client.get("/admin", headers=h)
    assert r.status_code == 200
    # Stat tile labels.
    assert "People" in r.text
    assert "Cohorts" in r.text
    assert "Courses" in r.text
    assert "Labs" in r.text
    # Links into the admin area.
    assert "/admin/users" in r.text
    assert "/admin/settings" in r.text


def test_instructor_forbidden_on_console(app_client, admin_session, tenant_a):
    _seed_login(admin_session, tenant_a, "inst@a.edu", "instructor")
    h = _login(app_client, "inst@a.edu")
    assert app_client.get("/admin", headers=h).status_code == 403


def test_student_forbidden_on_console(app_client, admin_session, tenant_a):
    _seed_login(admin_session, tenant_a, "stud@a.edu", "student")
    h = _login(app_client, "stud@a.edu")
    assert app_client.get("/admin", headers=h).status_code == 403


def test_admin_settings_still_works(app_client, admin_session, tenant_a):
    """Regression: GET /admin must not shadow /admin/settings.

    In the single-Academy deployment, the tenant admin role owns settings.
    """
    _seed_login(admin_session, tenant_a, "admin@a.edu", "admin")
    h = _login(app_client, "admin@a.edu")
    r = app_client.get("/admin/settings", headers=h)
    assert r.status_code == 200
