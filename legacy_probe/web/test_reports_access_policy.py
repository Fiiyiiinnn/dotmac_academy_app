"""Genuine hosted Reports canaries; no local execution or schema substitution.

Requires installed canonical identity owners, a004 physical FK compatibility and
whole factory closure. Fixture/migration/installed acceptance remains unqualified.
"""
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from dotmac_kernel.models import AuthSession, PartyRoleGrant, Role
from fastapi import HTTPException
from sqlalchemy import select, update

from app.config import Settings
from app.models.cohort import Cohort
from app.models.facilitator import FacilitatorNomination
from app.models.platform_settings import PlatformSetting
from app.services import settings_store
from app.services.identity import AcademyCurrentAccount
from app.services.reports_access import require_reports_access
from app.services.web_auth import require_web_user
from legacy_probe.conftest import grant_role, login_location
from legacy_probe.services.test_canonical_current_user import _request

KEY = "reports_access_roles"


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


def _nominate_cohort(a, cohort_id=None):
    """Hold an active whole-cohort nomination (the #188 cohort scope).

    The reports index and every cohort route need either global staff access or
    a cohort scope, so route-level success for a purpose-role holder is proved
    with a nomination. Returns ``(nomination_id, cohort_id)``.
    """
    if cohort_id is None:
        cohort = Cohort(tenant_id=a.tid, name=f"Reports {uuid4().hex}", discipline="networking", status="active")
        a.admin.add(cohort)
        a.admin.flush()
        cohort_id = cohort.id
    nomination = FacilitatorNomination(
        tenant_id=a.tid, person_id=a.pid, scope_type="cohort", cohort_id=cohort_id,
        status="active", activated_at=datetime.now(UTC),
    )
    a.admin.add(nomination)
    a.admin.commit()
    return nomination.id, cohort_id


def _withdraw_nomination(a, nomination_id):
    a.admin.delete(a.admin.get(FacilitatorNomination, nomination_id))
    a.admin.commit()


def _client(a, app_client):
    app_client.cookies.clear()
    app_client.cookies.set("session", a.token)
    headers = {"Host": "alpha.localhost"}
    app_client.get("/instructor/reports", headers=headers, follow_redirects=False)
    return {**headers, "x-csrf-token": app_client.cookies.get("csrf_token", "")}


def _all_routes(client, headers):
    cid = uuid4()
    paths = ["", f"/cohort/{cid}/analytics", f"/cohort/{cid}.csv", f"/cohort/{cid}",
             f"/student/{cid}", f"/cohort/{cid}/roster.csv", f"/cohort/{cid}/funnel.csv",
             f"/cohort/{cid}/queue.csv", "/academy-summary.csv"]
    responses = [client.get("/instructor/reports" + p, headers=headers, follow_redirects=False) for p in paths]
    responses.extend(client.post("/instructor/reports" + p, headers=headers, follow_redirects=False)
                     for p in [f"/student/{cid}/email", f"/cohort/{cid}/email"])
    return responses


def test_custom_role_live_policy_empty_deny_and_grant_withdrawal(current_account, app_client):
    a = current_account
    slug = f"settings-curator-{uuid4().hex}"
    grant_id = _grant(a, slug)
    settings_store.set_many(a.admin, {KEY: f'["{slug}"]'})
    a.admin.commit()
    request = _request(a.tenant, a.token)
    account = require_web_user(request, a.db)
    # The purpose policy alone admits the account at the service function; the
    # index route additionally needs a cohort scope (#188), held by a nomination.
    assert isinstance(require_reports_access(request, account, a.db), AcademyCurrentAccount)
    nomination_id, _ = _nominate_cohort(a)
    headers = _client(a, app_client)
    assert app_client.get("/instructor/reports", headers=headers).status_code == 200
    # Withdraw the scope: the empty-deny and grant-withdrawal below are then
    # proved with the policy as the only authority, never through the fallback.
    _withdraw_nomination(a, nomination_id)
    stale_policy = a.db.get(PlatformSetting, KEY)
    a.admin.execute(update(PlatformSetting).where(PlatformSetting.key == KEY).values(value="[]"))
    a.admin.commit()
    assert stale_policy.value == f'["{slug}"]'
    with pytest.raises(HTTPException) as caught:
        require_reports_access(request, account, a.db)
    assert caught.value.status_code == 403
    assert [r.status_code for r in _all_routes(app_client, headers)] == [403] * 11
    settings_store.set_many(a.admin, {KEY: f'["{slug}"]'})
    a.admin.commit()
    assert require_reports_access(request, account, a.db).id == a.pid
    stale_grant = a.db.get(PartyRoleGrant, grant_id)
    a.admin.delete(a.admin.get(PartyRoleGrant, grant_id))
    a.admin.commit()
    assert stale_grant.id == grant_id
    # Grant withdrawal is proved at the service function, where the purpose role
    # was the only authority: no nomination, no other grant names the policy.
    with pytest.raises(HTTPException) as withdrawn:
        require_reports_access(request, account, a.db)
    assert withdrawn.value.status_code == 403
    assert [r.status_code for r in _all_routes(app_client, headers)] == [403] * 11


def test_admin_reaches_reports_as_global_staff_and_foreign_tenant_roles_do_not_authorize(current_account, tenant_b):
    a = current_account
    grant_role(a.admin, tenant_id=a.tid, party_id=a.pid)
    settings_store.set_many(a.admin, {KEY: '["custom-settings-owner"]'})
    a.admin.add(Role(tenant_id=tenant_b.id, slug="custom-settings-owner", name="foreign role"))
    a.admin.commit()
    request = _request(a.tenant, a.token)
    account = require_web_user(request, a.db)
    # A role that exists only in another tenant never authorizes, and a plain
    # student holds neither the purpose role nor any cohort scope.
    with pytest.raises(HTTPException) as caught:
        require_reports_access(request, account, a.db)
    assert caught.value.status_code == 403
    # #188: the admin role is global staff access, so it reaches reports through
    # facilitator_access without being named in the purpose policy.
    _grant(a, "admin")
    assert require_reports_access(request, account, a.db).id == a.pid
    with pytest.raises(HTTPException) as caught:
        require_web_user(_request(tenant_b, a.token), a.db)
    assert caught.value.status_code == 303


def test_revoked_session_denies_every_reports_route(current_account, app_client):
    a = current_account
    _grant(a, "admin")
    settings_store.set_many(a.admin, {KEY: '["admin"]'})
    a.admin.commit()
    headers = _client(a, app_client)
    assert app_client.get("/instructor/reports", headers=headers).status_code == 200
    stale_session = a.db.get(AuthSession, a.session_id)
    a.admin.execute(update(AuthSession).where(AuthSession.id == a.session_id).values(revoked_at=datetime.now(UTC)))
    a.admin.commit()
    assert stale_session.revoked_at is None
    responses = _all_routes(app_client, headers)
    assert [r.status_code for r in responses] == [302] * 11
    assert all(r.headers["Location"] == login_location(r.request.url.path) for r in responses)


@pytest.mark.parametrize("raw", [None, "null", "", "{}", "true", '["owner","owner"]', '[" owner"]', '[1]'])
def test_malformed_reports_access_policy_refuses_config_and_store(current_account, raw):
    a = current_account
    with pytest.raises(ValueError, match=KEY):
        Settings(**{KEY: raw})
    with pytest.raises(ValueError, match=KEY):
        settings_store.set_many(a.admin, {KEY: raw})
    a.admin.merge(PlatformSetting(key=KEY, value=raw))
    a.admin.commit()
    request = _request(a.tenant, a.token)
    with pytest.raises(ValueError, match=KEY):
        require_reports_access(request, require_web_user(request, a.db), a.db)


def test_policy_env_custom_slug_and_empty_list(monkeypatch):
    monkeypatch.setenv("REPORTS_ACCESS_ROLES", '["operator"]')
    assert Settings().reports_access_roles == ("operator",)
    monkeypatch.setenv("SETTINGS_ACCESS_ROLES", '["manager"]')
    settings = Settings()
    assert settings.settings_access_roles == ("manager",)
    assert settings.reports_access_roles == ("operator",)
    assert Settings(**{KEY: []}).reports_access_roles == ()




@pytest.mark.parametrize("state", ["inactive", "suspended"])
def test_live_disabled_account_denies_every_reports_route(current_account, app_client, state):
    from dotmac_kernel.models import Party

    from app.models.identity_extensions import AcademyAccountProfile

    a = current_account
    _grant(a, "reports-reviewer")
    settings_store.set_many(a.admin, {KEY: '["reports-reviewer"]'})
    a.admin.commit()
    _nominate_cohort(a)
    headers = _client(a, app_client)
    assert app_client.get("/instructor/reports", headers=headers).status_code == 200
    if state == "inactive":
        a.admin.execute(update(Party).where(Party.id == a.pid).values(is_active=False))
    else:
        a.admin.execute(update(AcademyAccountProfile).where(AcademyAccountProfile.party_id == a.pid)
                        .values(status="suspended"))
    a.admin.commit()
    assert [r.status_code for r in _all_routes(app_client, headers)] == [303] * 11
