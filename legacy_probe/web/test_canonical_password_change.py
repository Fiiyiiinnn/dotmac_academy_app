"""Actual-a5 password route assertions with genuine canonical account setup.

Authored, NOT RUN. Requires the future owner/action enrollment, real canonical
assembly, app_user fixture and AccountToken FK retarget; no LegacyPerson shim.
Original tests/web/test_account.py remains intact in its legacy cohort.
"""

from dotmac_kernel.models import AuthSession
from sqlalchemy import select

from legacy_probe.conftest import login_location
from legacy_probe.web.test_canonical_self_profile import profile_client

assert profile_client


def _can_login(client, headers, csrf, email, password):
    response = client.post(
        "/login",
        headers={**headers, "x-csrf-token": csrf},
        data={"email": email, "password": password},
        follow_redirects=False,
    )
    return response.status_code == 303


def test_password_wrong_current_rejected(profile_client):
    a, app_client, h, csrf = profile_client
    r = app_client.post(
        "/account/password",
        headers={**h, "x-csrf-token": csrf},
        data={"current_password": "wrongpass", "new_password": "brandnew2", "confirm_password": "brandnew2"},
    )
    assert r.status_code == 200
    assert "error" in r.text.lower() or "incorrect" in r.text.lower()
    assert _can_login(app_client, h, csrf, a.email, "chosen-password9") is True
    assert _can_login(app_client, h, csrf, a.email, "brandnew2") is False


def test_password_change_success(profile_client):
    a, app_client, h, csrf = profile_client
    r = app_client.post(
        "/account/password",
        headers={**h, "x-csrf-token": csrf},
        data={"current_password": "chosen-password9", "new_password": "brandnew2", "confirm_password": "brandnew2"},
    )
    assert r.status_code == 200
    assert "updated" in r.text.lower() or "success" in r.text.lower()
    a.admin.rollback()
    sessions = a.admin.scalars(
        select(AuthSession).where(AuthSession.tenant_id == a.tid).where(AuthSession.party_id == a.pid)
    ).all()
    assert sessions and all(session.revoked_at is not None for session in sessions)
    revoked = app_client.get("/account", headers=h, follow_redirects=False)
    assert revoked.status_code == 302 and revoked.headers["Location"] == login_location("/account")
    assert _can_login(app_client, h, csrf, a.email, "brandnew2") is True
    assert _can_login(app_client, h, csrf, a.email, "chosen-password9") is False


def test_password_mismatch_or_short_rejected(profile_client):
    a, app_client, h, csrf = profile_client
    r = app_client.post(
        "/account/password",
        headers={**h, "x-csrf-token": csrf},
        data={"current_password": "chosen-password9", "new_password": "short", "confirm_password": "short"},
    )
    assert r.status_code == 200
    assert "error" in r.text.lower() or "least 8" in r.text.lower()
    r = app_client.post(
        "/account/password",
        headers={**h, "x-csrf-token": csrf},
        data={"current_password": "chosen-password9", "new_password": "brandnew2", "confirm_password": "different2"},
    )
    assert r.status_code == 200
    assert "error" in r.text.lower() or "match" in r.text.lower()
    assert _can_login(app_client, h, csrf, a.email, "chosen-password9") is True
