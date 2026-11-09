"""Tests for cookie-based web auth (Task 3)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.models.identity_extensions import AcademyCredentialPolicy
from legacy_probe.conftest import csrf_for, grant_role, make_person_credential, make_person_party, web_login


def test_login_sets_cookie_and_protects(app_client, admin_session, tenant_a):
    # Seed a Person + UserCredential for tenant_a directly.

    from app.services.security import hash_password

    p = make_person_party(admin_session, tenant_id=tenant_a.id, email="s@alpha.edu", first_name="Sam", last_name="Lee")
    admin_session.add(p)
    admin_session.flush()
    admin_session.add(
        make_person_credential(admin_session, tenant_id=tenant_a.id, party_id=p.id, email="s@alpha.edu", password_hash=hash_password("password1"))
    )
    grant_role(admin_session, tenant_id=tenant_a.id, party_id=p.id)
    admin_session.commit()

    h = {"Host": "alpha.localhost"}

    # (a) Unauthenticated GET of a protected route redirects to /login.
    r = app_client.get("/account", headers=h, follow_redirects=False)
    assert r.status_code in (302, 303)

    # The CSRF middleware sets a csrf_token cookie on every safe (GET) response.
    # Subsequent POSTs must double-submit it as the x-csrf-token header.
    csrf = r.cookies.get("csrf_token") or app_client.cookies.get("csrf_token", "")

    # (b) POST /login with valid form creds returns 303 and sets a session cookie.
    r = app_client.post(
        "/login",
        headers={**h, "x-csrf-token": csrf},
        data={"email": "s@alpha.edu", "password": "password1"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "session" in r.cookies

def test_login_canonicalizes_email_and_ignores_credential_email_drift(
    app_client,
    admin_session,
    tenant_a,
):

    from app.services.security import hash_password

    person = make_person_party(admin_session, tenant_id=tenant_a.id, email="Mixed.Case@Alpha.EDU ", first_name="Mixed", last_name="Case")
    admin_session.add(person)
    admin_session.flush()
    credential = make_person_credential(admin_session, tenant_id=tenant_a.id, party_id=person.id, email="stale-login@alpha.edu", password_hash=hash_password("password1"))
    admin_session.add(credential)
    grant_role(admin_session, tenant_id=tenant_a.id, party_id=person.id)
    admin_session.commit()

    response = web_login(app_client, {"Host": "alpha.localhost"}, "  MIXED.case@alpha.edu  ", "password1")

    assert response.status_code == 303
    admin_session.refresh(person)
    policy = admin_session.get(AcademyCredentialPolicy, credential.id)
    assert policy is not None
    admin_session.refresh(policy)
    assert person.email == "mixed.case@alpha.edu"
    assert policy.email == person.email

def test_logout_revokes_server_side_session(app_client, admin_session, tenant_a):
    """Logout must mark the AuthSession revoked; replaying the old cookie is rejected."""

    from app.services.security import hash_password

    p = make_person_party(admin_session, tenant_id=tenant_a.id, email="x@alpha.edu", first_name="X", last_name="Y")
    admin_session.add(p)
    admin_session.flush()
    admin_session.add(
        make_person_credential(admin_session, tenant_id=tenant_a.id, party_id=p.id, email="x@alpha.edu", password_hash=hash_password("pw1234"))
    )
    grant_role(admin_session, tenant_id=tenant_a.id, party_id=p.id)
    admin_session.commit()

    h = {"Host": "alpha.localhost"}

    # Obtain csrf_token.
    csrf = csrf_for(app_client, h, "/login")

    # Login — captures session cookie.
    r = app_client.post(
        "/login",
        headers={**h, "x-csrf-token": csrf},
        data={"email": "x@alpha.edu", "password": "pw1234"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "session" in r.cookies
    old_session_token = r.cookies["session"]

    # Logout. The session changed at login, so the pre-login token is no longer
    # valid; fetch the freshly issued session-bound token.
    csrf = csrf_for(app_client, h, "/account")
    r = app_client.post(
        "/logout",
        headers={**h, "x-csrf-token": csrf},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303)

    # Re-issue GET /account with the OLD session cookie — must redirect (session revoked).
    app_client.cookies.set("session", old_session_token)
    r = app_client.get(
        "/account",
        headers={**h},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303), "Revoked session must not authenticate"

def test_cross_tenant_cookie_rejected(app_client, admin_session, tenant_a, tenant_b):
    """A session cookie minted for tenant_a must not authenticate on tenant_b."""

    from app.services.security import hash_password

    p = make_person_party(admin_session, tenant_id=tenant_a.id, email="q@alpha.edu", first_name="Q", last_name="Z")
    admin_session.add(p)
    admin_session.flush()
    admin_session.add(
        make_person_credential(admin_session, tenant_id=tenant_a.id, party_id=p.id, email="q@alpha.edu", password_hash=hash_password("pw5678"))
    )
    grant_role(admin_session, tenant_id=tenant_a.id, party_id=p.id)
    admin_session.commit()

    h_a = {"Host": "alpha.localhost"}

    # Obtain csrf_token from tenant_a login page.
    r = app_client.get("/login", headers=h_a, follow_redirects=False)
    csrf = r.cookies.get("csrf_token") or app_client.cookies.get("csrf_token", "")

    # Login on tenant_a — capture session cookie.
    r = app_client.post(
        "/login",
        headers={**h_a, "x-csrf-token": csrf},
        data={"email": "q@alpha.edu", "password": "pw5678"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "session" in r.cookies
    session_token = r.cookies["session"]

    # Present that session cookie against tenant_b — must be rejected.
    h_b = {"Host": "beta.localhost"}
    replay = TestClient(app_client.app)
    replay.cookies.set("session", session_token)
    r = replay.get(
        "/account",
        headers=h_b,
        follow_redirects=False,
    )
    assert r.status_code in (302, 303), "Cross-tenant session must not authenticate"
