"""Hosted actual lab router and fresh canonical web dependency canaries.

No lab/entitlement or authentication dependency is replaced. Authored NOT RUN.
"""
from uuid import uuid4

from dotmac_kernel.models import Party
from dotmac_learning import ActivityWrite, CourseWrite, create_activity, create_course
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import update

from app.api.deps import get_db
from app.models.lab import LabInstance
from app.services.identity import AcademyCurrentAccount
from app.services.web_auth import require_web_user
from app.web import labs
from legacy_probe.services.test_canonical_current_user import _request, current_account

assert current_account


def _client(a):
    app = FastAPI()
    @app.middleware("http")
    async def tenant(request, call_next):
        request.state.tenant = a.tenant
        return await call_next(request)
    app.dependency_overrides[get_db] = lambda: a.db
    app.include_router(labs.router)
    client = TestClient(app, follow_redirects=False)
    client.cookies.set("session", a.token)
    return client


def _instance(a, tenant_id, person_id, activity_id=None):
    instance = LabInstance(tenant_id=tenant_id, person_id=person_id,
                           activity_id=activity_id or uuid4(), instance_name=f"test-{uuid4().hex}")
    a.admin.add(instance)
    a.admin.commit()
    return instance.id


def test_actual_router_dependency_keeps_canonical_id_and_fresh_refusal(current_account):
    a = current_account
    account = require_web_user(_request(a.tenant, a.token), a.db)
    assert isinstance(account, AcademyCurrentAccount) and account.id == a.pid
    client = _client(a)
    missing = f"/labs/instances/{uuid4()}/status"
    assert client.get(missing).status_code == 404
    a.admin.execute(update(Party).where(Party.id == a.pid).values(is_active=False))
    a.admin.commit()
    response = client.get(missing)
    assert response.status_code == 303 and response.headers["location"] == "/login"
    client.cookies.clear()
    assert client.get(missing).status_code == 303


def test_actual_router_preserves_tenant_owner_and_missing_activity_gates(current_account, tenant_b):
    a = current_account
    foreign = _instance(a, tenant_b.id, a.pid)
    other_owner = _instance(a, a.tid, uuid4())
    missing_activity = _instance(a, a.tid, a.pid)
    client = _client(a)
    assert client.get(f"/labs/instances/{foreign}/status").status_code == 404
    assert client.get(f"/labs/instances/{other_owner}/status").status_code == 403
    assert client.get(f"/labs/instances/{missing_activity}/status").status_code == 404


def test_actual_router_keeps_owner_entitlement_check(current_account):
    a = current_account
    course = create_course(a.admin, CourseWrite(
        id=uuid4(), tenant_id=a.tid, slug=uuid4().hex, title="Private Lab",
        discipline="test", source_ref="test", status="published", listed=False,
    ))
    activity = create_activity(a.admin, ActivityWrite(
        id=uuid4(), tenant_id=a.tid, course_id=course.id, title="Lab", type="lab",
        pass_threshold=0.5, assessment_mode="graded", grading="auto", weight=1,
    ))
    owned = _instance(a, a.tid, a.pid, activity.id)
    response = _client(a).get(f"/labs/instances/{owned}/status")
    assert response.status_code == 403
