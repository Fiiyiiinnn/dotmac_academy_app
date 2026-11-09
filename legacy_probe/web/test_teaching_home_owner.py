"""Ported teaching scenarios using genuine hosted owners and current cookie accounts.

Authored only: installed owner assembly and physical Party/Learning FK transition
are prerequisites. No runtime/qualification claim accompanies this source.
"""
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from dotmac_kernel.models import AuthSession, Party, PartyRoleGrant
from dotmac_learning.participation_models import Cohort, Enrollment
from fastapi import HTTPException
from sqlalchemy import update

from app.config import Settings
from app.models.identity_extensions import AcademyAccountProfile
from app.models.platform_settings import PlatformSetting
from app.services import settings_store
from app.services.identity import AcademyCurrentAccount
from app.services.teaching_access import require_teaching_access
from app.services.web_auth import require_web_user
from app.web import teaching as routes
from legacy_probe.conftest import login_location
from legacy_probe.services.test_canonical_current_user import current_account
from legacy_probe.services.test_notifications_owner import _party
from legacy_probe.web.test_bookmarks_owner import _request
from legacy_probe.web.test_reports_access_policy import _nominate_cohort
from legacy_probe.web.test_settings_access_policy import _grant

assert current_account
KEY = "teaching_access_roles"


@pytest.fixture(autouse=True)
def _restore_policy(request):
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


def _allow(a, slug=None):
    slug = slug or f"teaching-{uuid4().hex}"
    grant = _grant(a, slug)
    settings_store.set_many(a.admin, {KEY: f'["{slug}"]'})
    a.admin.commit()
    return grant, slug


def _render(a, limit=6, offset=0):
    request = _request(a.tenant, a.token)
    account = require_teaching_access(request, require_web_user(request, a.db), a.db)
    return routes.teaching_home(request, account, a.db, limit=limit, offset=offset)


def test_teaching_directory_exact_active_counts_paging_and_foreign_tenant(current_account, tenant_b):
    a = current_account
    _allow(a)
    prefix = uuid4().hex
    cohorts = [Cohort(tenant_id=a.tid, name=f"{prefix}-{i:02d}", discipline="Teaching") for i in range(8)]
    foreign = Cohort(tenant_id=tenant_b.id, name=f"{prefix}-foreign", discipline="Hidden")
    unnominated = Cohort(tenant_id=a.tid, name=f"{prefix}-unnominated", discipline="Teaching")
    a.admin.add_all([*cohorts, foreign, unnominated])
    a.admin.flush()
    active, suspended, inactive, withdrawn, facilitator = [_party(a, a.tid) for _ in range(5)]
    stamp = datetime.now(UTC)
    for party in (active, suspended, inactive, withdrawn, facilitator):
        a.admin.add(AcademyAccountProfile(
            party_id=party.id, tenant_id=a.tid, status="active", prefs={}, created_at=stamp, updated_at=stamp,
        ))
    a.admin.flush()
    a.admin.execute(update(AcademyAccountProfile).where(AcademyAccountProfile.party_id == suspended.id).values(status="suspended"))
    a.admin.execute(update(Party).where(Party.id == inactive.id).values(is_active=False))
    for party, status, role in [(active, "active", "student"), (suspended, "active", "student"),
                                (inactive, "active", "student"), (withdrawn, "withdrawn", "student"),
                                (facilitator, "active", "instructor")]:
        a.admin.add(Enrollment(tenant_id=a.tid, cohort_id=cohorts[0].id, person_id=party.id,
                               role_in_cohort=role, status=status))
    a.admin.commit()
    # A purpose-role account sees only nominated cohorts (no global staff access).
    for cohort in cohorts:
        _nominate_cohort(a, cohort.id)
    require_web_user(_request(a.tenant, a.token), a.db)
    root = a.db.get_transaction()
    first = _render(a, limit=50)
    rows = first.context["cohorts"]
    assert unnominated.id not in {row["cohort"].id for row in rows}
    assert len(rows) == 8
    expected = sorted([row["cohort"].name for row in rows])
    assert [row["cohort"].name for row in rows] == expected
    assert next(row["count"] for row in rows if row["cohort"].id == cohorts[0].id) == 1
    assert foreign.id not in {row["cohort"].id for row in rows}
    assert {c.id for c in cohorts}.issubset({row["cohort"].id for row in rows})
    page = _render(a, limit=2, offset=2)
    assert [row["cohort"].id for row in page.context["cohorts"]] == [row["cohort"].id for row in rows[2:4]]
    assert _render(a, limit=2, offset=100000).context["cohorts"] == []
    assert b"Teaching home" in first.body and b"Quick links" in first.body
    assert f"/instructor/cohorts/{cohorts[0].id}".encode() in first.body
    assert b"/instructor/reports" in first.body
    assert a.db.get_transaction() is root
    assert page.context["pagination"]["total"] == len(rows)


def test_custom_teaching_roles_live_empty_deny_and_revoked_grant(current_account, app_client):
    a = current_account
    grant, slug = _allow(a)
    app_client.cookies.set("session", a.token)
    headers = {"Host": "alpha.localhost"}
    assert app_client.get("/instructor", headers=headers).status_code == 200
    request = _request(a.tenant, a.token)
    account = require_web_user(request, a.db)
    assert isinstance(require_teaching_access(request, account, a.db), AcademyCurrentAccount)
    stale_policy = a.db.get(PlatformSetting, KEY)
    a.admin.execute(update(PlatformSetting).where(PlatformSetting.key == KEY).values(value="[]"))
    a.admin.commit()
    assert stale_policy.value == f'["{slug}"]'
    assert app_client.get("/instructor", headers=headers).status_code == 403
    settings_store.set_many(a.admin, {KEY: f'["{slug}"]'})
    a.admin.commit()
    stale_grant = a.db.get(PartyRoleGrant, grant)
    a.admin.delete(a.admin.get(PartyRoleGrant, grant))
    a.admin.commit()
    assert stale_grant.id == grant
    assert app_client.get("/instructor", headers=headers).status_code == 403


@pytest.mark.parametrize("mutation", ["revoked", "inactive", "suspended"])
def test_teaching_live_cookie_status_and_session_refuse(current_account, app_client, mutation):
    a = current_account
    _allow(a)
    app_client.cookies.set("session", a.token)
    headers = {"Host": "alpha.localhost"}
    assert app_client.get("/instructor", headers=headers).status_code == 200
    if mutation == "revoked":
        a.admin.execute(update(AuthSession).where(AuthSession.id == a.session_id).values(revoked_at=datetime.now(UTC)))
    elif mutation == "inactive":
        a.admin.execute(update(Party).where(Party.id == a.pid).values(is_active=False))
    else:
        a.admin.execute(update(AcademyAccountProfile).where(AcademyAccountProfile.party_id == a.pid).values(status="suspended"))
    a.admin.commit()
    denied = app_client.get("/instructor", headers=headers, follow_redirects=False)
    if mutation == "revoked":
        # A revoked session is refused by the kernel facet auth with the exact
        # login redirect for the requested path.
        assert denied.status_code == 302 and denied.headers["Location"] == login_location("/instructor")
    else:
        assert denied.status_code == 303 and denied.headers["Location"] == "/login"


def test_teaching_policy_separate_from_settings_and_foreign_cookie(current_account, tenant_b):
    a = current_account
    # M15 / #188: an admin grant reaches teaching as global staff through the
    # facilitator fallback, so it cannot prove the policy is separate. A custom
    # settings-only role holds no teaching authority of its own.
    _grant(a, "settings-only")
    settings_store.set_many(a.admin, {KEY: '["teaching-custom"]', "settings_access_roles": '["settings-only"]'})
    a.admin.commit()
    request = _request(a.tenant, a.token)
    with pytest.raises(HTTPException) as denied:
        require_teaching_access(request, require_web_user(request, a.db), a.db)
    assert denied.value.status_code == 403
    # Positive control: an active cohort nomination (the #188 scope) admits the
    # same account, so the refusal above was the policy and not the account.
    _nominate_cohort(a)
    assert require_teaching_access(request, require_web_user(request, a.db), a.db).id == a.pid
    with pytest.raises(HTTPException) as foreign:
        require_web_user(_request(tenant_b, a.token), a.db)
    assert foreign.value.status_code == 303


@pytest.mark.parametrize("raw", [None, "null", "", "{}", "true", '["owner","owner"]', '[" owner"]', '[1]'])
def test_malformed_teaching_policy_refuses_config_store_and_existing_row(current_account, raw):
    a = current_account
    with pytest.raises(ValueError, match=KEY):
        Settings(**{KEY: raw})
    with pytest.raises(ValueError, match=KEY):
        settings_store.set_many(a.admin, {KEY: raw})
    a.admin.merge(PlatformSetting(key=KEY, value=raw))
    a.admin.commit()
    request = _request(a.tenant, a.token)
    with pytest.raises(ValueError, match=KEY):
        require_teaching_access(request, require_web_user(request, a.db), a.db)


def test_teaching_env_custom_and_empty_policy(monkeypatch):
    monkeypatch.setenv("TEACHING_ACCESS_ROLES", '["custom-tutor"]')
    assert Settings().teaching_access_roles == ("custom-tutor",)
    assert Settings(**{KEY: []}).teaching_access_roles == ()


@pytest.mark.parametrize("slug,expected", [("instructor", 200), ("admin", 200), ("student", 403)])
def test_historical_default_role_access_on_exact_teaching_path(current_account, app_client, slug, expected):
    import json

    a = current_account
    _grant(a, slug)
    defaults = Settings.model_fields[KEY].default
    settings_store.set_many(a.admin, {KEY: json.dumps(defaults)})
    a.admin.commit()
    app_client.cookies.set("session", a.token)
    response = app_client.get("/instructor", headers={"Host": "alpha.localhost"})
    assert response.status_code == expected
    if expected == 200:
        assert "Teaching home" in response.text
        assert "/instructor/cohorts" in response.text
