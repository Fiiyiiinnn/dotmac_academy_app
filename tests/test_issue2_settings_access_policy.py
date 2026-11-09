"""Maintained port of legacy_probe/web/test_settings_access_policy.py.

Disposition: owner ``app/services/settings_access.py``; maintained test
``test_settings_access_fresh_role_every_route``.

Adaptations: genesis ``current_account`` + bare-router client (see
tests/issue2_wave3_support.py); ``PlatformSetting`` rows are kernel
``DomainSetting`` rows; ``smtp_password`` is env-only by decision, so
stored-password sub-cases are dropped and the never-echoed assertion
runs against the env secret (plus: no stored row may appear); a revoked
cookie surfaces as ``require_web_user``'s 303 ``/login`` (the kernel
facet 302 of the full assembly is out of scope here).
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from dotmac_kernel.models import AuthSession, Party, PartyRoleGrant, Role
from fastapi import HTTPException
from sqlalchemy import text, update
from sqlalchemy.exc import ProgrammingError

from app.config import Settings
from app.config import settings as app_settings
from app.services import settings_store
from app.services.email import EmailResult
from app.services.identity import AcademyCurrentAccount
from app.services.settings_access import require_settings_access
from app.services.web_auth import require_web_user
from app.web import settings as routes
from tests.issue2_wave3_support import (
    current_account,
    grant_custom,
    make_client,
    make_request,
    raw_setting_row,
    store_raw_setting,
    tenant_b,
)

assert current_account and tenant_b
KEY = "settings_access_roles"


def _client(a):
    return make_client(a, routes.router)


def _all_routes(client):
    return [
        client.get("/admin/settings", follow_redirects=False),
        client.post("/admin/settings", data={"branding_name": "denied"}, follow_redirects=False),
        client.post("/admin/settings/test-email", follow_redirects=False),
    ]


def test_custom_role_live_policy_empty_deny_and_grant_withdrawal(current_account):
    a = current_account
    slug = f"settings-curator-{uuid4().hex}"
    grant_id = grant_custom(a, slug)
    settings_store.set_many(a.admin, {KEY: f'["{slug}"]'})
    a.admin.commit()
    request = make_request(a.tenant, a.token)
    account = require_web_user(request, a.db)
    assert isinstance(require_settings_access(request, account, a.db), AcademyCurrentAccount)
    client = _client(a)
    assert client.get("/admin/settings").status_code == 200
    stale_policy = raw_setting_row(a.db, KEY)
    store_raw_setting(a.admin, KEY, "[]")
    assert stale_policy.value_json == [slug]
    with pytest.raises(HTTPException) as caught:
        require_settings_access(request, account, a.db)
    assert caught.value.status_code == 403
    assert [r.status_code for r in _all_routes(client)] == [403, 403, 403]
    settings_store.set_many(a.admin, {KEY: f'["{slug}"]'})
    a.admin.commit()
    assert require_settings_access(request, account, a.db).id == a.pid
    stale_grant = a.db.get(PartyRoleGrant, grant_id)
    a.admin.delete(a.admin.get(PartyRoleGrant, grant_id))
    a.admin.commit()
    assert stale_grant.id == grant_id
    assert [r.status_code for r in _all_routes(client)] == [403, 403, 403]


def test_admin_has_no_implicit_override_and_foreign_tenant_roles_do_not_authorize(current_account, tenant_b):
    a = current_account
    grant_custom(a, "admin")
    settings_store.set_many(a.admin, {KEY: '["custom-settings-owner"]'})
    a.admin.add(Role(tenant_id=tenant_b.id, slug="custom-settings-owner", name="foreign role"))
    a.admin.commit()
    request = make_request(a.tenant, a.token)
    account = require_web_user(request, a.db)
    with pytest.raises(HTTPException) as caught:
        require_settings_access(request, account, a.db)
    assert caught.value.status_code == 403
    with pytest.raises(HTTPException) as caught:
        require_web_user(make_request(tenant_b, a.token), a.db)
    assert caught.value.status_code == 303


def test_revoked_session_denies_every_settings_route(current_account):
    a = current_account
    grant_custom(a, "admin")
    settings_store.set_many(a.admin, {KEY: '["admin"]'})
    a.admin.commit()
    client = _client(a)
    assert client.get("/admin/settings").status_code == 200
    stale_session = a.db.get(AuthSession, a.session_id)
    a.admin.execute(update(AuthSession).where(AuthSession.id == a.session_id).values(revoked_at=datetime.now(UTC)))
    a.admin.commit()
    assert stale_session.revoked_at is None
    responses = _all_routes(client)
    assert [r.status_code for r in responses] == [303, 303, 303]
    assert all(r.headers["Location"] == "/login" for r in responses)


@pytest.mark.parametrize("raw", [None, "null", "", "{}", "true", '["owner","owner"]', '[" owner"]', "[1]"])
def test_malformed_settings_access_policy_refuses_config_and_store(current_account, raw):
    a = current_account
    with pytest.raises(ValueError, match=KEY):
        Settings(**{KEY: raw})
    with pytest.raises(ValueError, match=KEY):
        settings_store.set_many(a.admin, {KEY: raw})
    a.admin.rollback()
    store_raw_setting(a.admin, KEY, raw)
    request = make_request(a.tenant, a.token)
    with pytest.raises(ValueError, match=KEY):
        require_settings_access(request, require_web_user(request, a.db), a.db)


def test_policy_env_custom_slug_and_empty_list(monkeypatch):
    monkeypatch.setenv("SETTINGS_ACCESS_ROLES", '["operator"]')
    assert Settings().settings_access_roles == ("operator",)
    assert Settings(**{KEY: []}).settings_access_roles == ()


def test_form_hx_blank_password_and_current_account_branded_email(current_account, monkeypatch):
    a = current_account
    grant_custom(a, "admin")
    secret = f"secret-{uuid4().hex}"
    monkeypatch.setattr(app_settings, "smtp_password", secret)
    brand = "Example <Academy & Co>"
    settings_store.set_many(a.admin, {KEY: '["admin"]', "smtp_host": "smtp.example.test", "branding_name": brand})
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
    client = _client(a)
    get = client.get("/admin/settings")
    assert get.status_code == 200 and secret not in get.text
    assert all("smtp_password" not in context.get("cfg", {}) for context in captures)
    data = {
        "branding_name": brand,
        "smtp_host": "smtp.example.test",
        "smtp_port": "587",
        "smtp_password": "",
        "smtp_user": "sender@example.test",
        "smtp_from": "",
        "max_concurrent_labs": "20",
        "max_concurrent_labs_per_tenant": "0",
        "lab_idle_minutes": "60",
    }
    response = client.post("/admin/settings", headers={"HX-Request": "true"}, data=data)
    assert write_roles and all(role != "app_user" for role in write_roles)
    assert response.status_code == 200 and "Settings saved" in response.text and secret not in response.text
    a.admin.expire_all()
    # smtp_password is env-only: the save must not create a stored row.
    assert raw_setting_row(a.admin, "smtp_password") is None
    assert settings_store.get_all(a.admin).get("smtp_from") == "sender@example.test"
    assert "smtp_password" not in captures[-1]["cfg"]
    response = client.post("/admin/settings", data=data, follow_redirects=False)
    assert response.status_code == 303 and response.headers["Location"] == "/admin/settings"
    error = client.post("/admin/settings/test-email")
    assert error.status_code == 200 and secret not in error.text and "Delivery failed" in error.text
    args, kwargs = calls[-1]
    assert args[0] == changed_email and args[0] != a.login_snapshot.email
    assert args[1] == f"{brand} — test email"
    assert "Example &lt;Academy &amp; Co&gt;" in args[2] and "<Academy" not in args[2]
    assert brand in kwargs["text_body"] and kwargs["db"] is not None
    assert all("smtp_password" not in context.get("cfg", {}) for context in captures)
    invalid = client.post(
        "/admin/settings",
        headers={"HX-Request": "true"},
        data={**data, "smtp_password": secret, "max_concurrent_labs": "-1"},
    )
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
