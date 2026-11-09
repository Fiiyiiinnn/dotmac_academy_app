"""Maintained port of legacy_probe/web/test_reports_access_policy.py.

Disposition: owner ``app/services/reports_access.py``; maintained test
``test_reports_access_fresh_on_all_11_routes``.

Adaptations: genesis ``current_account`` + bare-router client (see
tests/issue2_wave3_support.py); ``PlatformSetting`` rows are kernel
``DomainSetting`` rows; a revoked cookie surfaces as
``require_web_user``'s 303 ``/login`` on every route.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from dotmac_kernel.models import AuthSession, Party, PartyRoleGrant, Role
from fastapi import HTTPException
from sqlalchemy import update

from app.config import Settings
from app.models.cohort import Cohort
from app.models.facilitator import FacilitatorNomination
from app.models.identity_extensions import AcademyAccountProfile
from app.services import settings_store
from app.services.identity import AcademyCurrentAccount
from app.services.reports_access import require_reports_access
from app.services.web_auth import require_web_user
from app.web import reports as routes
from tests.issue2_wave3_support import (
    current_account,
    grant_custom,
    grant_std,
    make_client,
    make_request,
    raw_setting_row,
    store_raw_setting,
    tenant_b,
)

assert current_account and tenant_b
KEY = "reports_access_roles"


def nominate_cohort(a, cohort_id=None):
    """Hold an active whole-cohort nomination (the #188 cohort scope)."""
    if cohort_id is None:
        cohort = Cohort(tenant_id=a.tid, name=f"Reports {uuid4().hex}", discipline="networking", status="active")
        a.admin.add(cohort)
        a.admin.flush()
        cohort_id = cohort.id
    nomination = FacilitatorNomination(
        tenant_id=a.tid,
        person_id=a.pid,
        scope_type="cohort",
        cohort_id=cohort_id,
        status="active",
        activated_at=datetime.now(UTC),
    )
    a.admin.add(nomination)
    a.admin.commit()
    return nomination.id, cohort_id


def withdraw_nomination(a, nomination_id):
    a.admin.delete(a.admin.get(FacilitatorNomination, nomination_id))
    a.admin.commit()


def _client(a):
    return make_client(a, routes.router)


def _all_routes(client):
    cid = uuid4()
    paths = [
        "",
        f"/cohort/{cid}/analytics",
        f"/cohort/{cid}.csv",
        f"/cohort/{cid}",
        f"/student/{cid}",
        f"/cohort/{cid}/roster.csv",
        f"/cohort/{cid}/funnel.csv",
        f"/cohort/{cid}/queue.csv",
        "/academy-summary.csv",
    ]
    responses = [client.get("/instructor/reports" + p, follow_redirects=False) for p in paths]
    responses.extend(
        client.post("/instructor/reports" + p, follow_redirects=False)
        for p in [f"/student/{cid}/email", f"/cohort/{cid}/email"]
    )
    return responses


def test_custom_role_live_policy_empty_deny_and_grant_withdrawal(current_account):
    a = current_account
    slug = f"settings-curator-{uuid4().hex}"
    grant_id = grant_custom(a, slug)
    settings_store.set_many(a.admin, {KEY: f'["{slug}"]'})
    a.admin.commit()
    request = make_request(a.tenant, a.token)
    account = require_web_user(request, a.db)
    assert isinstance(require_reports_access(request, account, a.db), AcademyCurrentAccount)
    nomination_id, _ = nominate_cohort(a)
    client = _client(a)
    assert client.get("/instructor/reports").status_code == 200
    withdraw_nomination(a, nomination_id)
    stale_policy = raw_setting_row(a.db, KEY)
    store_raw_setting(a.admin, KEY, "[]")
    assert stale_policy.value_json == [slug]
    with pytest.raises(HTTPException) as caught:
        require_reports_access(request, account, a.db)
    assert caught.value.status_code == 403
    assert [r.status_code for r in _all_routes(client)] == [403] * 11
    settings_store.set_many(a.admin, {KEY: f'["{slug}"]'})
    a.admin.commit()
    assert require_reports_access(request, account, a.db).id == a.pid
    stale_grant = a.db.get(PartyRoleGrant, grant_id)
    a.admin.delete(a.admin.get(PartyRoleGrant, grant_id))
    a.admin.commit()
    assert stale_grant.id == grant_id
    with pytest.raises(HTTPException) as withdrawn:
        require_reports_access(request, account, a.db)
    assert withdrawn.value.status_code == 403
    assert [r.status_code for r in _all_routes(client)] == [403] * 11


def test_admin_reaches_reports_as_global_staff_and_foreign_tenant_roles_do_not_authorize(current_account, tenant_b):
    a = current_account
    grant_std(a.admin, tenant_id=a.tid, party_id=a.pid)
    a.admin.commit()
    settings_store.set_many(a.admin, {KEY: '["custom-settings-owner"]'})
    a.admin.add(Role(tenant_id=tenant_b.id, slug="custom-settings-owner", name="foreign role"))
    a.admin.commit()
    request = make_request(a.tenant, a.token)
    account = require_web_user(request, a.db)
    with pytest.raises(HTTPException) as caught:
        require_reports_access(request, account, a.db)
    assert caught.value.status_code == 403
    grant_custom(a, "admin")
    assert require_reports_access(request, account, a.db).id == a.pid
    with pytest.raises(HTTPException) as caught:
        require_web_user(make_request(tenant_b, a.token), a.db)
    assert caught.value.status_code == 303


def test_revoked_session_denies_every_reports_route(current_account):
    a = current_account
    grant_custom(a, "admin")
    settings_store.set_many(a.admin, {KEY: '["admin"]'})
    a.admin.commit()
    client = _client(a)
    assert client.get("/instructor/reports").status_code == 200
    stale_session = a.db.get(AuthSession, a.session_id)
    a.admin.execute(update(AuthSession).where(AuthSession.id == a.session_id).values(revoked_at=datetime.now(UTC)))
    a.admin.commit()
    assert stale_session.revoked_at is None
    responses = _all_routes(client)
    assert [r.status_code for r in responses] == [303] * 11
    assert all(r.headers["Location"] == "/login" for r in responses)


@pytest.mark.parametrize("raw", [None, "null", "", "{}", "true", '["owner","owner"]', '[" owner"]', "[1]"])
def test_malformed_reports_access_policy_refuses_config_and_store(current_account, raw):
    a = current_account
    with pytest.raises(ValueError, match=KEY):
        Settings(**{KEY: raw})
    with pytest.raises(ValueError, match=KEY):
        settings_store.set_many(a.admin, {KEY: raw})
    a.admin.rollback()
    store_raw_setting(a.admin, KEY, raw)
    request = make_request(a.tenant, a.token)
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
def test_live_disabled_account_denies_every_reports_route(current_account, state):
    a = current_account
    grant_custom(a, "reports-reviewer")
    settings_store.set_many(a.admin, {KEY: '["reports-reviewer"]'})
    a.admin.commit()
    nominate_cohort(a)
    client = _client(a)
    assert client.get("/instructor/reports").status_code == 200
    if state == "inactive":
        a.admin.execute(update(Party).where(Party.id == a.pid).values(is_active=False))
    else:
        a.admin.execute(
            update(AcademyAccountProfile).where(AcademyAccountProfile.party_id == a.pid).values(status="suspended")
        )
    a.admin.commit()
    assert [r.status_code for r in _all_routes(client)] == [303] * 11
