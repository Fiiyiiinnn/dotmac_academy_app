"""Genuine hosted Settings canaries; no local execution or schema substitution.

Requires installed canonical identity owners, a004 physical FK compatibility and
whole factory closure. Fixture/migration/installed acceptance remains unqualified.
"""
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from dotmac_kernel.models import AuthSession, Party, PartyRoleGrant, Role
from fastapi import HTTPException
from sqlalchemy import select, text, update
from sqlalchemy.exc import ProgrammingError

from app.config import Settings
from app.models.platform_settings import PlatformSetting
from app.services import settings_store
from app.services.email import EmailResult
from app.services.identity import AcademyCurrentAccount
from app.services.settings_access import require_settings_access
from app.services.web_auth import require_web_user
from app.web import settings as routes
from legacy_probe.conftest import grant_role, login_location
from legacy_probe.services.test_canonical_current_user import _request

KEY = "settings_access_roles"


@pytest.fixture(autouse=True)
def _restore_settings(request):
    if "current_account" not in request.fixturenames:
        yield
        return
    a = request.getfixturevalue("current_account")
    original = settings_store.get_all(a.admin)
    try:
        yield
    finally:
        a.db.rollback()
        a.admin.rollback()
        a.admin.query(PlatformSetting).delete()
        for key, value in original.items():
            a.admin.add(PlatformSetting(key=key, value=value))
        a.admin.commit()


def _grant(a, slug, tenant_id=None):
    # A custom purpose role never admits anyone to the web facet by itself (only
    # student/instructor/admin do), so the account also holds the standard student
    # role. The policy-level refusals under test are unchanged: student is not in
    # any *_access_roles policy.
    if tenant_id is None and slug not in {"student", "instructor", "admin"}:
        grant_role(a.admin, tenant_id=a.tid, party_id=a.pid)
    tid = tenant_id or a.tid
    role = a.admin.scalars(select(Role).where(Role.tenant_id == tid, Role.slug == slug)).first()
    if role is None:
        role = Role(tenant_id=tid, slug=slug, name=slug)
        a.admin.add(role)
        a.admin.flush()
    grant = PartyRoleGrant(tenant_id=tid, party_id=a.pid, role_id=role.id)
    a.admin.add(grant)
    a.admin.commit()
    return grant.id


def _client(a, app_client):
    app_client.cookies.clear()
    app_client.cookies.set("session", a.token)
    headers = {"Host": "alpha.localhost"}
    app_client.get("/admin/settings", headers=headers, follow_redirects=False)
    return {**headers, "x-csrf-token": app_client.cookies.get("csrf_token", "")}


def _all_routes(client, headers):
    return [client.get("/admin/settings", headers=headers, follow_redirects=False),
            client.post("/admin/settings", data={"branding_name": "denied"}, headers=headers,
                        follow_redirects=False),
            client.post("/admin/settings/test-email", headers=headers, follow_redirects=False)]


def test_custom_role_live_policy_empty_deny_and_grant_withdrawal(current_account, app_client):
    a = current_account
    slug = f"settings-curator-{uuid4().hex}"
    grant_id = _grant(a, slug)
    settings_store.set_many(a.admin, {KEY: f'["{slug}"]'})
    a.admin.commit()
    request = _request(a.tenant, a.token)
    account = require_web_user(request, a.db)
    assert isinstance(require_settings_access(request, account, a.db), AcademyCurrentAccount)
    headers = _client(a, app_client)
    assert app_client.get("/admin/settings", headers=headers).status_code == 200
    stale_policy = a.db.get(PlatformSetting, KEY)
    a.admin.execute(update(PlatformSetting).where(PlatformSetting.key == KEY).values(value="[]"))
    a.admin.commit()
    assert stale_policy.value == f'["{slug}"]'
    with pytest.raises(HTTPException) as caught:
        require_settings_access(request, account, a.db)
    assert caught.value.status_code == 403
    assert [r.status_code for r in _all_routes(app_client, headers)] == [403, 403, 403]
    settings_store.set_many(a.admin, {KEY: f'["{slug}"]'})
    a.admin.commit()
    assert require_settings_access(request, account, a.db).id == a.pid
    stale_grant = a.db.get(PartyRoleGrant, grant_id)
    a.admin.delete(a.admin.get(PartyRoleGrant, grant_id))
    a.admin.commit()
    assert stale_grant.id == grant_id
    assert [r.status_code for r in _all_routes(app_client, headers)] == [403, 403, 403]


def test_admin_has_no_implicit_override_and_foreign_tenant_roles_do_not_authorize(current_account, tenant_b):
    a = current_account
    _grant(a, "admin")
    settings_store.set_many(a.admin, {KEY: '["custom-settings-owner"]'})
    a.admin.add(Role(tenant_id=tenant_b.id, slug="custom-settings-owner", name="foreign role"))
    a.admin.commit()
    request = _request(a.tenant, a.token)
    account = require_web_user(request, a.db)
    with pytest.raises(HTTPException) as caught:
        require_settings_access(request, account, a.db)
    assert caught.value.status_code == 403
    with pytest.raises(HTTPException) as caught:
        require_web_user(_request(tenant_b, a.token), a.db)
    assert caught.value.status_code == 303


def test_revoked_session_denies_every_settings_route(current_account, app_client):
    a = current_account
    _grant(a, "admin")
    settings_store.set_many(a.admin, {KEY: '["admin"]'})
    a.admin.commit()
    headers = _client(a, app_client)
    assert app_client.get("/admin/settings", headers=headers).status_code == 200
    stale_session = a.db.get(AuthSession, a.session_id)
    a.admin.execute(update(AuthSession).where(AuthSession.id == a.session_id).values(revoked_at=datetime.now(UTC)))
    a.admin.commit()
    assert stale_session.revoked_at is None
    responses = _all_routes(app_client, headers)
    assert [r.status_code for r in responses] == [302, 302, 302]
    assert all(r.headers["Location"] == login_location(r.request.url.path) for r in responses)


@pytest.mark.parametrize("raw", [None, "null", "", "{}", "true", '["owner","owner"]', '[" owner"]', '[1]'])
def test_malformed_settings_access_policy_refuses_config_and_store(current_account, raw):
    a = current_account
    with pytest.raises(ValueError, match=KEY):
        Settings(**{KEY: raw})
    with pytest.raises(ValueError, match=KEY):
        settings_store.set_many(a.admin, {KEY: raw})
    a.admin.merge(PlatformSetting(key=KEY, value=raw))
    a.admin.commit()
    request = _request(a.tenant, a.token)
    with pytest.raises(ValueError, match=KEY):
        require_settings_access(request, require_web_user(request, a.db), a.db)


def test_policy_env_custom_slug_and_empty_list(monkeypatch):
    monkeypatch.setenv("SETTINGS_ACCESS_ROLES", '["operator"]')
    assert Settings().settings_access_roles == ("operator",)
    assert Settings(**{KEY: []}).settings_access_roles == ()


def test_form_hx_blank_password_and_current_account_branded_email(current_account, app_client, monkeypatch):
    a = current_account
    _grant(a, "admin")
    secret = f"secret-{uuid4().hex}"
    brand = "Example <Academy & Co>"
    settings_store.set_many(a.admin, {KEY: '["admin"]', "smtp_password": secret,
                                    "smtp_host": "smtp.example.test", "branding_name": brand})
    changed_email = f"current-{uuid4().hex}@example.test"
    a.admin.execute(update(Party).where(Party.id == a.pid).values(email=changed_email))
    a.admin.commit()
    captures = []
    original_template = routes.templates.TemplateResponse

    def capture_template(*args, **kwargs):
        context = args[2]
        captures.append(context)
        return original_template(*args, **kwargs)

    monkeypatch.setattr(routes.templates, "TemplateResponse", capture_template)
    calls = []

    def delivery(*args, **kwargs):
        calls.append((args, kwargs))
        return EmailResult(False, f"unsafe transport diagnostic {secret}")

    monkeypatch.setattr(routes, "send_email_detailed", delivery)
    write_roles = []
    original_set_many = routes.set_many

    def capture_writer(db, values):
        write_roles.append(db.scalar(text("SELECT current_user")))
        return original_set_many(db, values)

    monkeypatch.setattr(routes, "set_many", capture_writer)
    headers = _client(a, app_client)
    get = app_client.get("/admin/settings", headers=headers)
    assert get.status_code == 200 and secret not in get.text
    assert all("smtp_password" not in context.get("cfg", {}) for context in captures)
    data = {"branding_name": brand, "smtp_host": "smtp.example.test", "smtp_port": "587",
            "smtp_password": "", "smtp_user": "sender@example.test", "smtp_from": "",
            "max_concurrent_labs": "20", "max_concurrent_labs_per_tenant": "0",
              "lab_idle_minutes": "60"}
    response = app_client.post("/admin/settings", headers={**headers, "HX-Request": "true"}, data=data)
    assert write_roles and all(role != "app_user" for role in write_roles)
    assert response.status_code == 200 and "Settings saved" in response.text and secret not in response.text
    a.admin.expire_all()
    assert a.admin.get(PlatformSetting, "smtp_password").value == secret
    assert a.admin.get(PlatformSetting, "smtp_from").value == "sender@example.test"
    assert "smtp_password" not in captures[-1]["cfg"]
    response = app_client.post("/admin/settings", headers=headers, data=data, follow_redirects=False)
    assert response.status_code == 303 and response.headers["Location"] == "/admin/settings"
    error = app_client.post("/admin/settings/test-email", headers=headers)
    assert error.status_code == 200 and secret not in error.text and "Delivery failed" in error.text
    args, kwargs = calls[-1]
    assert args[0] == changed_email and args[0] != a.login_snapshot.email
    assert args[1] == f"{brand} — test email"
    assert "Example &lt;Academy &amp; Co&gt;" in args[2] and "<Academy" not in args[2]
    assert brand in kwargs["text_body"] and kwargs["db"] is not None
    assert all("smtp_password" not in context.get("cfg", {}) for context in captures)
    invalid = app_client.post("/admin/settings", headers={**headers, "HX-Request": "true"},
                              data={**data, "smtp_password": secret, "max_concurrent_labs": "-1"})
    assert invalid.status_code == 422 and secret not in invalid.text


def test_ordinary_role_cannot_write_and_writer_helper_never_commits(current_account):
    a = current_account
    assert a.db.scalar(text("SELECT current_user")) == "app_user"
    with pytest.raises(ProgrammingError):
        with a.db.begin_nested():
            settings_store.set_many(a.db, {"branding_name": "ordinary denied"})
    a.admin.execute(text("SELECT 1"))
    root = a.admin.get_transaction()
    settings_store.set_many(a.admin, {"branding_name": "caller rollback"})
    assert a.admin.get_transaction() is root
    a.admin.rollback()
    assert settings_store.effective(a.db, require_store=True).branding_name != "caller rollback"
