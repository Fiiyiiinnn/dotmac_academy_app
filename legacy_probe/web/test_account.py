"""Tests for the Account area — profile + password change (Task 7)."""

from __future__ import annotations

from sqlalchemy import select

from app.models.auth import AuthSession
from app.models.identity_extensions import AcademyAccountProfile
from app.services.security import hash_password
from legacy_probe.conftest import grant_role, login_location, make_person_credential, make_person_party, web_login


def _login(app_client, admin_session, tenant, password="password1"):
    p = make_person_party(admin_session, tenant_id=tenant.id, email="s@a.edu", first_name="S", last_name="L")
    admin_session.add(p)
    admin_session.flush()
    admin_session.add(
        make_person_credential(admin_session, tenant_id=tenant.id, party_id=p.id, email="s@a.edu", password_hash=hash_password(password))
    )
    grant_role(admin_session, tenant_id=tenant.id, party_id=p.id)
    admin_session.commit()
    h = {"Host": "alpha.localhost"}
    # Log in via the CSRF double-submit flow (web_login).
    web_login(app_client, h, "s@a.edu", password)
    csrf = app_client.cookies.get("csrf_token", "")
    return p, h, csrf

def _can_login(app_client, h, csrf, password):
    """Drive /login and report whether the credentials are accepted."""
    r = app_client.post(
        "/login",
        headers={**h, "x-csrf-token": csrf},
        data={"email": "s@a.edu", "password": password},
        follow_redirects=False,
    )
    return r.status_code == 303

def test_profile_update_changes_name(app_client, admin_session, tenant_a):
    _p, h, csrf = _login(app_client, admin_session, tenant_a)

    r = app_client.post(
        "/account",
        headers={**h, "x-csrf-token": csrf},
        data={"first_name": "Newfirst", "last_name": "Newlast"},
    )
    assert r.status_code == 200

    r = app_client.get("/account", headers=h)
    assert r.status_code == 200
    assert "Newfirst" in r.text
    assert "Newlast" in r.text
    # Email is read-only / unchanged.
    assert "s@a.edu" in r.text

def test_password_wrong_current_rejected(app_client, admin_session, tenant_a):
    _p, h, csrf = _login(app_client, admin_session, tenant_a)

    r = app_client.post(
        "/account/password",
        headers={**h, "x-csrf-token": csrf},
        data={
            "current_password": "wrongpass",
            "new_password": "brandnew2",
            "confirm_password": "brandnew2",
        },
    )
    assert r.status_code == 200
    assert "error" in r.text.lower() or "incorrect" in r.text.lower()

    # Old password still works; the new one was NOT applied.
    assert _can_login(app_client, h, csrf, "password1") is True
    assert _can_login(app_client, h, csrf, "brandnew2") is False

def test_password_change_success(app_client, admin_session, tenant_a):
    person, h, csrf = _login(app_client, admin_session, tenant_a)

    r = app_client.post(
        "/account/password",
        headers={**h, "x-csrf-token": csrf},
        data={
            "current_password": "password1",
            "new_password": "brandnew2",
            "confirm_password": "brandnew2",
        },
    )
    assert r.status_code == 200
    assert "updated" in r.text.lower() or "success" in r.text.lower()

    admin_session.rollback()
    sessions = admin_session.scalars(
        select(AuthSession).where(AuthSession.tenant_id == tenant_a.id).where(AuthSession.party_id == person.id)
    ).all()
    assert sessions and all(session.revoked_at is not None for session in sessions)
    revoked = app_client.get("/account", headers=h, follow_redirects=False)
    assert revoked.status_code == 302 and revoked.headers["Location"] == login_location("/account")

    # New password works; old does not.
    assert _can_login(app_client, h, csrf, "brandnew2") is True
    assert _can_login(app_client, h, csrf, "password1") is False

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64

def test_avatar_upload_sets_path(app_client, admin_session, tenant_a):
    p, h, csrf = _login(app_client, admin_session, tenant_a)

    r = app_client.post(
        "/account/avatar",
        headers={**h, "x-csrf-token": csrf},
        files={"file": ("me.png", _PNG, "image/png")},
    )
    assert r.status_code == 200
    assert r.headers.get("HX-Refresh") == "true"

    # avatar_path now persisted + rendered on the profile page.
    admin_session.expire_all()
    profile = admin_session.get(AcademyAccountProfile, p.id)
    assert profile.avatar_path == f"/static/avatars/{tenant_a.id}/{p.id}.png"

    r = app_client.get("/account", headers=h)
    assert "/static/avatars/" in r.text

def test_avatar_upload_rejects_non_image(app_client, admin_session, tenant_a):
    p, h, csrf = _login(app_client, admin_session, tenant_a)

    r = app_client.post(
        "/account/avatar",
        headers={**h, "x-csrf-token": csrf},
        files={"file": ("notes.txt", b"not an image", "text/plain")},
    )
    assert r.status_code == 200
    assert r.headers.get("HX-Refresh") is None
    assert "image" in r.text.lower() or "PNG" in r.text

    admin_session.expire_all()
    assert admin_session.get(AcademyAccountProfile, p.id).avatar_path is None

def test_notifications_toggle_persists(app_client, admin_session, tenant_a):
    p, h, csrf = _login(app_client, admin_session, tenant_a)

    # Submit with only digest checked => results opted OUT, digest opted IN.
    r = app_client.post(
        "/account/notifications",
        headers={**h, "x-csrf-token": csrf},
        data={"email_digest": "1"},
    )
    assert r.status_code == 200

    admin_session.expire_all()
    prefs = admin_session.get(AcademyAccountProfile, p.id).prefs
    assert prefs.get("email_results") is False
    assert prefs.get("email_digest") is True

    # Form reflects stored state (digest box checked, results unchecked).
    r = app_client.get("/account/notifications", headers=h)
    assert r.status_code == 200
    assert "Result emails" in r.text

def test_password_mismatch_or_short_rejected(app_client, admin_session, tenant_a):
    _p, h, csrf = _login(app_client, admin_session, tenant_a)

    # Too short.
    r = app_client.post(
        "/account/password",
        headers={**h, "x-csrf-token": csrf},
        data={"current_password": "password1", "new_password": "short", "confirm_password": "short"},
    )
    assert r.status_code == 200
    assert "error" in r.text.lower() or "least 8" in r.text.lower()

    # Mismatch.
    r = app_client.post(
        "/account/password",
        headers={**h, "x-csrf-token": csrf},
        data={
            "current_password": "password1",
            "new_password": "brandnew2",
            "confirm_password": "different2",
        },
    )
    assert r.status_code == 200
    assert "error" in r.text.lower() or "match" in r.text.lower()

    # Password unchanged — original still logs in.
    assert _can_login(app_client, h, csrf, "password1") is True
