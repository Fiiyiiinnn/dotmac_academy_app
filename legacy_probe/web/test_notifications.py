"""Web-layer tests for /notifications."""
from __future__ import annotations

from sqlalchemy import text

from app.services import notifications as notif_svc
from app.services.security import hash_password
from legacy_probe.conftest import grant_role, login_location, make_person_credential, make_person_party, web_login

H = {"Host": "alpha.localhost"}

def _make_user(admin_session, tenant, email="notif_web@a.edu"):
    p = make_person_party(admin_session, tenant_id=tenant.id, email=email, first_name="N", last_name="W")
    admin_session.add(p)
    admin_session.flush()
    admin_session.add(
        make_person_credential(admin_session, tenant_id=tenant.id, party_id=p.id, email=email, password_hash=hash_password("pw1"))
    )
    grant_role(admin_session, tenant_id=tenant.id, party_id=p.id)
    admin_session.commit()
    return p

def _set_tenant(db, tenant_id):
    db.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(tenant_id)})

def test_notifications_requires_auth(app_client, tenant_a):
    """GET /notifications without a session redirects to /login."""
    # tenant_a ensures "alpha" subdomain resolves in the DB
    r = app_client.get("/notifications", headers=H, follow_redirects=False)
    # The kernel facet auth redirects (302) before Academy's own 303 guard.
    assert r.status_code == 302
    assert r.headers["location"] == login_location("/notifications")

def test_notifications_list_200(app_client, admin_session, tenant_a):
    """Authenticated user gets 200 on /notifications."""
    _make_user(admin_session, tenant_a, "notif_list@a.edu")
    web_login(app_client, H, "notif_list@a.edu", "pw1")

    r = app_client.get("/notifications", headers=H)
    assert r.status_code == 200
    assert "Notifications" in r.text

def test_notifications_shows_items(app_client, admin_session, tenant_a):
    """Notifications created for the user appear on the page."""
    p = _make_user(admin_session, tenant_a, "notif_items@a.edu")
    _set_tenant(admin_session, tenant_a.id)
    notif_svc.notify(
        admin_session,
        tenant_id=tenant_a.id,
        person_id=p.id,
        kind="result",
        title="You passed the quiz",
    )
    admin_session.commit()

    web_login(app_client, H, "notif_items@a.edu", "pw1")
    r = app_client.get("/notifications", headers=H)
    assert r.status_code == 200
    assert "You passed the quiz" in r.text

def test_unread_badge_count_service(admin_session, tenant_a):
    """unread_count returns the right number (service-level assertion for badge)."""
    from uuid import uuid4

    _set_tenant(admin_session, tenant_a.id)
    pid = uuid4()
    p2 = make_person_party(admin_session, tenant_id=tenant_a.id, email=f"{pid}@badge.test", first_name="B", last_name="G")
    pid = p2.id
    admin_session.add(p2)
    admin_session.flush()

    assert notif_svc.unread_count(admin_session, tenant_id=tenant_a.id, person_id=pid) == 0
    notif_svc.notify(admin_session, tenant_id=tenant_a.id, person_id=pid,
                     kind="certificate", title="Cert ready")
    admin_session.flush()
    assert notif_svc.unread_count(admin_session, tenant_id=tenant_a.id, person_id=pid) == 1

def test_read_all_post(app_client, admin_session, tenant_a):
    """POST /notifications/read-all marks notifications read and redirects."""
    p = _make_user(admin_session, tenant_a, "notif_rall@a.edu")
    _set_tenant(admin_session, tenant_a.id)
    notif_svc.notify(admin_session, tenant_id=tenant_a.id, person_id=p.id,
                     kind="result", title="Mark me read")
    admin_session.commit()

    # Obtain csrf_token via GET before login
    r_get = app_client.get("/login", headers=H)
    csrf = r_get.cookies.get("csrf_token") or app_client.cookies.get("csrf_token", "")

    app_client.post("/login", headers={**H, "x-csrf-token": csrf},
                    data={"email": "notif_rall@a.edu", "password": "pw1"})

    # Refresh csrf token (login GET may have set one; use latest)
    csrf = app_client.cookies.get("csrf_token", csrf)

    r = app_client.post("/notifications/read-all",
                        headers={**H, "x-csrf-token": csrf},
                        follow_redirects=False)
    assert r.status_code == 303

    _set_tenant(admin_session, tenant_a.id)
    assert notif_svc.unread_count(admin_session, tenant_id=tenant_a.id, person_id=p.id) == 0
