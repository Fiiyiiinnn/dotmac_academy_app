"""Maintained port of legacy_probe/web/test_settings.py.

Disposition: owner ``app/web/settings.py``; maintained test
``test_settings_admin_gate_and_save_validation``.

Adaptations: the signed-in genesis account takes each role in turn
(the legacy ``seed_login_with_account`` pattern) instead of password
login through the full assembly; settings rows are kernel
``DomainSetting`` rows read through ``settings_store``;
``smtp_password`` is env-only by decision — stored-password sub-cases
are dropped, the never-echoed assertion runs against the env secret,
and a save must never create a stored ``smtp_password`` row.
"""

from __future__ import annotations

from app.config import settings as app_settings
from app.models.assessment import Activity, Score, Submission
from app.models.course import Course
from app.services import email as email_mod
from app.services import settings_store
from app.services.email import notify_score_if_first_pass
from app.services.settings_store import set_many
from app.web import settings as routes
from tests.issue2_wave3_support import current_account, grant_custom, make_client, make_person_party, raw_setting_row

assert current_account


def _client(a, role_slug):
    grant_custom(a, role_slug)
    return make_client(a, routes.router)


def test_student_forbidden(current_account):
    client = _client(current_account, "student")
    assert client.get("/admin/settings").status_code == 403


def test_instructor_forbidden(current_account):
    client = _client(current_account, "instructor")
    assert client.get("/admin/settings").status_code == 403


def test_admin_get_ok_password_not_echoed(current_account, monkeypatch):
    monkeypatch.setattr(app_settings, "smtp_password", "s3cr3t-stored")
    client = _client(current_account, "admin")
    r = client.get("/admin/settings")
    assert r.status_code == 200
    assert "s3cr3t-stored" not in r.text


def test_admin_post_persists_branding(current_account):
    client = _client(current_account, "admin")
    r = client.post(
        "/admin/settings",
        data={
            "branding_name": "Renamed Academy",
            "smtp_port": "587",
            "max_concurrent_labs": "20",
            "max_concurrent_labs_per_tenant": "0",
            "lab_idle_minutes": "60",
        },
        follow_redirects=False,
    )
    assert r.status_code in (200, 303)
    again = client.get("/admin/settings")
    assert "Renamed Academy" in again.text


def test_admin_post_blank_password_keeps_existing(current_account, monkeypatch):
    # Env-only secret: a blank password field stores nothing and the
    # env secret is never echoed; other fields still persist.
    monkeypatch.setattr(app_settings, "smtp_password", "keep-me")
    a = current_account
    set_many(a.admin, {"smtp_host": "h.example"})
    a.admin.commit()
    client = _client(a, "admin")
    r = client.post(
        "/admin/settings",
        data={
            "branding_name": "X",
            "smtp_password": "",
            "smtp_host": "h.example",
            "smtp_port": "587",
            "max_concurrent_labs": "20",
            "max_concurrent_labs_per_tenant": "0",
            "lab_idle_minutes": "60",
        },
        follow_redirects=False,
    )
    assert r.status_code in (200, 303)
    assert "keep-me" not in r.text
    assert raw_setting_row(a.admin, "smtp_password") is None
    assert settings_store.get_all(a.admin).get("smtp_host") == "h.example"


def test_admin_post_rejects_negative_per_tenant_lab_limit(current_account):
    a = current_account
    client = _client(a, "admin")
    response = client.post(
        "/admin/settings",
        data={
            "branding_name": "Dotmac Academy",
            "smtp_port": "587",
            "max_concurrent_labs": "20",
            "max_concurrent_labs_per_tenant": "-1",
            "lab_idle_minutes": "60",
        },
    )
    assert response.status_code == 422
    assert raw_setting_row(a.admin, "max_concurrent_labs_per_tenant") is None


def test_test_email_invokes_send_email(current_account, monkeypatch):
    a = current_account
    set_many(a.admin, {"smtp_host": "smtp.example"})
    a.admin.commit()
    calls = []
    from app.services.email import EmailResult

    def _fake_send(*args, **kwargs):
        calls.append(kwargs.get("db") is not None)
        return EmailResult(True, None)

    monkeypatch.setattr(routes, "send_email_detailed", _fake_send)
    client = _client(a, "admin")
    r = client.post("/admin/settings/test-email")
    assert r.status_code == 200
    assert len(calls) == 1
    assert "Test email sent" in r.text


def test_test_email_unconfigured_message(current_account):
    client = _client(current_account, "admin")
    r = client.post("/admin/settings/test-email")
    assert r.status_code == 200
    assert "SMTP is not configured" in r.text


def test_email_auto_on_pass_toggle_off_skips_send(current_account, monkeypatch):
    """Wiring: with email_auto_on_pass=false, notify_score_if_first_pass skips."""
    a = current_account
    admin_session, tid = a.admin, a.tid
    calls = []
    monkeypatch.setattr(email_mod, "send_email", lambda *args, **kwargs: calls.append(args) or True)
    set_many(admin_session, {"email_auto_on_pass": "false"})

    c = Course(
        tenant_id=tid,
        slug="net",
        title="Net",
        discipline="networking",
        source_ref="x",
        version=1,
        status="published",
        listed=False,
    )
    admin_session.add(c)
    admin_session.flush()
    act = Activity(
        tenant_id=tid,
        course_id=c.id,
        chapter_number=1,
        type="mcq_test",
        title="Ch1",
        pass_threshold=0.6,
        grading="auto",
        weight=1.0,
    )
    admin_session.add(act)
    admin_session.flush()
    p = make_person_party(admin_session, tenant_id=tid, email="learner@stu.edu", first_name="L", last_name="N")
    sub = Submission(tenant_id=tid, activity_id=act.id, person_id=p.id, answers={}, attempt_no=1)
    admin_session.add(sub)
    admin_session.flush()
    s = Score(
        tenant_id=tid,
        submission_id=sub.id,
        score=10,
        max_score=10,
        fraction=1.0,
        passed=True,
        per_item=[],
        source="auto",
    )
    admin_session.add(s)
    admin_session.flush()

    sent = notify_score_if_first_pass(admin_session, score=s, activity=act, person=p)
    assert sent is False
    assert calls == []
    admin_session.rollback()
