"""Hosted genuine routes, cookie/grant policies and Learning reads. Authored NOT RUN."""
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from dotmac_kernel.models import AuthSession, Party, PartyRoleGrant
from dotmac_learning import CourseWrite, create_course
from dotmac_learning.participation_models import Cohort
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import update

from app.api.deps import get_db
from app.config import Settings
from app.models.identity_extensions import AcademyAccountProfile
from app.models.lab import LabInstance
from app.models.platform_settings import PlatformSetting
from app.services import settings_store
from app.services.api_identity_reads import count_all_people, person_contacts
from app.web import admin_home, lab_admin, reminders_admin
from legacy_probe.services.test_canonical_current_user import current_account
from legacy_probe.services.test_notifications_owner import _party
from legacy_probe.web.test_settings_access_policy import _grant

assert current_account
KEYS = ("admin_home_access_roles", "lab_admin_access_roles", "reminders_admin_access_roles")
PATHS = ("/admin", "/instructor/labs", "/admin/reminders")


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
        for key in KEYS:
            row = a.admin.get(PlatformSetting, key)
            if key in original:
                a.admin.merge(PlatformSetting(key=key, value=original[key]))
            elif row is not None:
                a.admin.delete(row)
        a.admin.commit()


def _client(a):
    app = FastAPI()
    @app.middleware("http")
    async def tenant(request, call_next):
        request.state.tenant = a.tenant
        return await call_next(request)
    app.dependency_overrides[get_db] = lambda: a.db
    for module in (admin_home, lab_admin, reminders_admin):
        app.include_router(module.router)
    client = TestClient(app, follow_redirects=False)
    client.cookies.set("session", a.token)
    return client


def _allow(a):
    slug = f"product-admin-{uuid4().hex}"
    grant = _grant(a, slug)
    settings_store.set_many(a.admin, {key: f'["{slug}"]' for key in KEYS})
    a.admin.commit()
    return grant, slug


def test_actual_routes_custom_roles_empty_deny_and_live_grant(current_account):
    a = current_account
    grant, slug = _allow(a)
    client = _client(a)
    assert [client.get(path).status_code for path in PATHS] == [200, 200, 200]
    settings_store.set_many(a.admin, {KEYS[0]: '[]'})
    a.admin.commit()
    assert [client.get(path).status_code for path in PATHS] == [403, 200, 200]
    settings_store.set_many(a.admin, {KEYS[0]: f'["{slug}"]'})
    a.admin.commit()
    a.db.get(PartyRoleGrant, grant)
    a.admin.delete(a.admin.get(PartyRoleGrant, grant))
    a.admin.commit()
    assert [client.get(path).status_code for path in PATHS] == [403, 403, 403]
    assert client.post(f"/admin/reminders/{uuid4()}/resend").status_code == 403
    assert client.post(f"/instructor/labs/scores/{uuid4()}/override",
                       data={"score_value": 1, "max_score": 2, "reason": "test"}).status_code == 403


@pytest.mark.parametrize("mutation", ["session", "inactive", "suspended"])
def test_actual_routes_fresh_cookie_status(current_account, mutation):
    a = current_account
    _allow(a)
    client = _client(a)
    assert client.get(PATHS[0]).status_code == 200
    if mutation == "session":
        a.admin.execute(update(AuthSession).where(AuthSession.id == a.session_id).values(revoked_at=datetime.now(UTC)))
    elif mutation == "inactive":
        a.admin.execute(update(Party).where(Party.id == a.pid).values(is_active=False))
    else:
        a.admin.execute(update(AcademyAccountProfile).where(AcademyAccountProfile.party_id == a.pid).values(status="suspended"))
    a.admin.commit()
    for path in PATHS:
        response = client.get(path)
        assert response.status_code == 303 and response.headers["location"] == "/login"


@pytest.mark.parametrize("key", KEYS)
@pytest.mark.parametrize("raw", [None, "null", "", "{}", "true", '["owner","owner"]', '[" owner"]', '[1]'])
def test_malformed_policy_refuses_config_and_store(current_account, key, raw):
    a = current_account
    with pytest.raises(ValueError):
        Settings(**{key: raw})
    with pytest.raises(ValueError):
        settings_store.set_many(a.admin, {key: raw})
    a.admin.merge(PlatformSetting(key=key, value=raw))
    a.admin.commit()
    with pytest.raises(ValueError):
        settings_store.effective(a.db, require_store=True)


def test_console_counts_all_people_all_learning_rows_and_product_labs(current_account, tenant_b):
    a = current_account
    _allow(a)
    baseline = count_all_people(a.db, tenant_id=a.tid)
    inactive = _party(a, a.tid)
    a.admin.execute(update(Party).where(Party.id == inactive.id).values(is_active=False))
    foreign = _party(a, tenant_b.id)
    cohort = Cohort(tenant_id=a.tid, name=uuid4().hex, discipline="all", status="inactive")
    a.admin.add(cohort)
    course = create_course(a.admin, CourseWrite(id=uuid4(), tenant_id=a.tid, slug=uuid4().hex,
        title="Draft course", discipline="all", source_ref="hosted", status="draft", listed=False))
    lab = LabInstance(tenant_id=a.tid, person_id=inactive.id, activity_id=uuid4(),
        instance_name=uuid4().hex, status="active")
    a.admin.add(lab)
    a.admin.commit()
    client = _client(a)
    response = client.get("/admin")
    assert response.status_code == 200
    assert count_all_people(a.db, tenant_id=a.tid) == baseline + 1
    from legacy_probe.services.test_canonical_current_user import _request
    request = _request(a.tenant, a.token)
    # The rendered page reads request.url, which needs a path in the scope.
    request.scope.update({"path": "/admin", "query_string": b""})
    result = admin_home.admin_console(request, a.db)
    import dotmac_learning as learning
    assert result.context["stats"]["people"] == baseline + 1
    assert result.context["stats"]["courses"] == len(learning.all_courses(a.db, tenant_id=a.tid))
    assert result.context["stats"]["cohorts"] == len(learning.instructor_cohort_choices(a.db, tenant_id=a.tid))
    assert course.id in {row.id for row in learning.all_courses(a.db, tenant_id=a.tid)}
    assert cohort.id in {row.id for row in learning.instructor_cohort_choices(a.db, tenant_id=a.tid)}
    assert result.context["stats"]["labs"] >= 1
    monitor = client.get("/instructor/labs")
    assert monitor.status_code == 200 and inactive.email.encode() in monitor.content
    assert foreign.email.encode() not in monitor.content
    assert person_contacts(a.db, tenant_id=tenant_b.id, person_ids=[inactive.id]) == {}


def test_missing_reminder_and_missing_override_use_actual_owners(current_account):
    a = current_account
    _allow(a)
    client = _client(a)
    response = client.post(f"/admin/reminders/{uuid4()}/resend")
    assert response.status_code == 200 and b"reminder not found" in response.content
    # Historical route propagates the product owner's missing-submission ValueError.
    with pytest.raises(ValueError, match="submission not found for tenant"):
        client.post(f"/instructor/labs/scores/{uuid4()}/override",
            data={"score_value": 1, "max_score": 2, "reason": "test"})
