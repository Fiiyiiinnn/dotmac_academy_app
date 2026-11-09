"""Tests for the platform admin Settings portal (admin-gated) + wiring."""

from __future__ import annotations

import pytest
from dotmac_kernel.models import PartyRoleGrant

from app.models.assessment import Activity, Score, Submission
from app.models.course import Course
from app.models.platform_settings import PlatformSetting
from app.services import email as email_mod
from app.services.bootstrap import ensure_roles
from app.services.email import notify_score_if_first_pass
from app.services.security import hash_password
from app.services.settings_store import set_many
from legacy_probe.conftest import make_person_credential, make_person_party, web_login


@pytest.fixture(autouse=True)
def _clean_platform_settings(admin_session):
    """Keep the platform-wide settings table empty around each test.

    The POST handler COMMITS rows (via platform_api); without cleanup those rows
    would leak into other tests that read effective() (lab limits, email toggles).
    """
    admin_session.query(PlatformSetting).delete()
    admin_session.commit()
    yield
    admin_session.rollback()
    admin_session.query(PlatformSetting).delete()
    admin_session.commit()

def _seed_login(app_client, admin_session, tenant, email, role_slug):
    roles = ensure_roles(admin_session, tenant.id)
    p = make_person_party(admin_session, tenant_id=tenant.id, email=email, first_name="Ad", last_name="Min")
    admin_session.add(p)
    admin_session.flush()
    admin_session.add(
        make_person_credential(admin_session, tenant_id=tenant.id, party_id=p.id, email=email, password_hash=hash_password("password1"))
    )
    admin_session.add(PartyRoleGrant(tenant_id=tenant.id, party_id=p.id, role_id=roles[role_slug].id))
    admin_session.commit()
    h = {"Host": "alpha.localhost"}
    web_login(app_client, h, email, "password1")
    return h

def test_student_forbidden(app_client, admin_session, tenant_a):
    h = _seed_login(app_client, admin_session, tenant_a, "stu@a.edu", "student")
    assert app_client.get("/admin/settings", headers=h).status_code == 403

def test_instructor_forbidden(app_client, admin_session, tenant_a):
    h = _seed_login(app_client, admin_session, tenant_a, "ins@a.edu", "instructor")
    assert app_client.get("/admin/settings", headers=h).status_code == 403

def test_admin_get_ok_password_not_echoed(app_client, admin_session, tenant_a):
    set_many(admin_session, {"smtp_password": "s3cr3t-stored"})
    admin_session.commit()
    h = _seed_login(app_client, admin_session, tenant_a, "adm@a.edu", "admin")
    r = app_client.get("/admin/settings", headers=h)
    assert r.status_code == 200
    assert "s3cr3t-stored" not in r.text

def test_admin_post_persists_branding(app_client, admin_session, tenant_a):
    h = _seed_login(app_client, admin_session, tenant_a, "adm@a.edu", "admin")
    app_client.get("/admin/settings", headers=h)
    csrf = app_client.cookies.get("csrf_token", "")
    r = app_client.post(
        "/admin/settings",
        headers={**h, "x-csrf-token": csrf},
        data={"branding_name": "Renamed Academy", "smtp_port": "587",
              "max_concurrent_labs": "20", "max_concurrent_labs_per_tenant": "0",
              "lab_idle_minutes": "60"},
        follow_redirects=False,
    )
    assert r.status_code in (200, 303)
    again = app_client.get("/admin/settings", headers=h)
    assert "Renamed Academy" in again.text

def test_admin_post_blank_password_keeps_existing(app_client, admin_session, tenant_a):
    set_many(admin_session, {"smtp_password": "keep-me", "smtp_host": "h.example"})
    admin_session.commit()
    h = _seed_login(app_client, admin_session, tenant_a, "adm@a.edu", "admin")
    app_client.get("/admin/settings", headers=h)
    csrf = app_client.cookies.get("csrf_token", "")
    app_client.post(
        "/admin/settings",
        headers={**h, "x-csrf-token": csrf},
        data={"branding_name": "X", "smtp_password": "", "smtp_host": "h.example",
              "smtp_port": "587", "max_concurrent_labs": "20", "max_concurrent_labs_per_tenant": "0",
              "lab_idle_minutes": "60"},
        follow_redirects=False,
    )
    row = admin_session.get(PlatformSetting, "smtp_password")
    assert row is not None and row.value == "keep-me"

def test_admin_post_rejects_negative_per_tenant_lab_limit(
    app_client, admin_session, tenant_a
):
    h = _seed_login(app_client, admin_session, tenant_a, "cap-admin@a.edu", "admin")
    app_client.get("/admin/settings", headers=h)
    csrf = app_client.cookies.get("csrf_token", "")
    response = app_client.post(
        "/admin/settings",
        headers={**h, "x-csrf-token": csrf},
        data={
            "branding_name": "Dotmac Academy",
            "smtp_port": "587",
            "max_concurrent_labs": "20",
            "max_concurrent_labs_per_tenant": "-1",
            "lab_idle_minutes": "60",
        },
    )
    assert response.status_code == 422
    assert (
        admin_session.get(PlatformSetting, "max_concurrent_labs_per_tenant")
        is None
    )

def test_test_email_invokes_send_email(app_client, admin_session, tenant_a, monkeypatch):
    set_many(admin_session, {"smtp_host": "smtp.example"})
    admin_session.commit()
    calls = []
    import app.web.settings as settings_web
    from app.services.email import EmailResult

    def _fake_send(*a, **k):
        calls.append(k.get("db") is not None)
        return EmailResult(True, None)

    monkeypatch.setattr(settings_web, "send_email_detailed", _fake_send)

    h = _seed_login(app_client, admin_session, tenant_a, "adm@a.edu", "admin")
    app_client.get("/admin/settings", headers=h)
    csrf = app_client.cookies.get("csrf_token", "")
    r = app_client.post("/admin/settings/test-email", headers={**h, "x-csrf-token": csrf})
    assert r.status_code == 200
    assert len(calls) == 1
    assert "Test email sent" in r.text

def test_test_email_unconfigured_message(app_client, admin_session, tenant_a):
    # No smtp_host stored, and env default is empty in the test config.
    h = _seed_login(app_client, admin_session, tenant_a, "adm@a.edu", "admin")
    app_client.get("/admin/settings", headers=h)
    csrf = app_client.cookies.get("csrf_token", "")
    r = app_client.post("/admin/settings/test-email", headers={**h, "x-csrf-token": csrf})
    assert r.status_code == 200
    assert "SMTP is not configured" in r.text

def test_email_auto_on_pass_toggle_off_skips_send(admin_session, tenant_a, monkeypatch):
    """Wiring: with email_auto_on_pass=false, notify_score_if_first_pass skips."""
    calls = []
    monkeypatch.setattr(email_mod, "send_email",
                        lambda *a, **k: calls.append(a) or True)
    set_many(admin_session, {"email_auto_on_pass": "false"})

    c = Course(tenant_id=tenant_a.id, slug="net", title="Net", discipline="networking",
               source_ref="x", version=1, status="published", listed=False)
    admin_session.add(c)
    admin_session.flush()
    act = Activity(tenant_id=tenant_a.id, course_id=c.id, chapter_number=1, type="mcq_test",
                   title="Ch1", pass_threshold=0.6, grading="auto", weight=1.0)
    admin_session.add(act)
    admin_session.flush()
    p = make_person_party(admin_session, tenant_id=tenant_a.id, email="learner@stu.edu", first_name="L", last_name="N")
    admin_session.add(p)
    admin_session.flush()
    sub = Submission(tenant_id=tenant_a.id, activity_id=act.id, person_id=p.id, answers={},
                     attempt_no=1)
    admin_session.add(sub)
    admin_session.flush()
    s = Score(tenant_id=tenant_a.id, submission_id=sub.id, score=10, max_score=10,
              fraction=1.0, passed=True, per_item=[], source="auto")
    admin_session.add(s)
    admin_session.flush()

    sent = notify_score_if_first_pass(admin_session, score=s, activity=act, person=p)
    assert sent is False
    assert calls == []
    admin_session.rollback()
