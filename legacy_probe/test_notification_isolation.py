"""RLS isolation tests for notifications.

Person B (same tenant) and Tenant B (different tenant) must not see Person A's
notifications through app_user-level queries (RLS enforced).
"""
from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.services import notifications as notif_svc
from legacy_probe.conftest import make_person_party


def _admin_set_tenant(db, tenant_id):
    db.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(tenant_id)})

@pytest.fixture(scope="module")
def app_user_engine():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    engine = create_engine(url, future=True)
    yield engine
    engine.dispose()

@pytest.fixture
def app_user_session(app_user_engine):
    SessionLocal = sessionmaker(bind=app_user_engine, autocommit=False, autoflush=False)
    db = SessionLocal()
    try:
        yield db
        db.rollback()
    finally:
        db.close()

def _make_person(admin_session, tenant):
    pid = uuid4()
    p = make_person_party(admin_session, tenant_id=tenant.id, email=f"{pid}@iso.test", first_name="I", last_name="S")
    pid = p.id
    admin_session.add(p)
    admin_session.flush()
    return p

def test_person_b_cannot_see_person_a_notification(admin_session, app_user_session, tenant_a):
    """Person B querying via app_user (RLS) cannot see Person A's notification."""
    _admin_set_tenant(admin_session, tenant_a.id)
    person_a = _make_person(admin_session, tenant_a)
    person_b = _make_person(admin_session, tenant_a)

    notif_svc.notify(
        admin_session,
        tenant_id=tenant_a.id,
        person_id=person_a.id,
        kind="result",
        title="Person A's private notification",
    )
    admin_session.commit()

    # Query as person_b via app_user session (RLS active)
    app_user_session.execute(
        text("SELECT set_config('app.current_tenant', :t, true)"),
        {"t": str(tenant_a.id)},
    )
    items = notif_svc.recent(app_user_session, tenant_id=tenant_a.id, person_id=person_b.id)
    titles = [n.title for n in items]
    assert "Person A's private notification" not in titles

def test_tenant_b_cannot_see_tenant_a_notification(
    admin_session, app_user_session, tenant_a, tenant_b
):
    """Tenant B's session cannot see Tenant A's notifications (different tenant_id)."""
    _admin_set_tenant(admin_session, tenant_a.id)
    person_a = _make_person(admin_session, tenant_a)
    notif_svc.notify(
        admin_session,
        tenant_id=tenant_a.id,
        person_id=person_a.id,
        kind="result",
        title="Tenant A secret notification",
    )
    admin_session.commit()

    _admin_set_tenant(admin_session, tenant_b.id)
    person_b2 = _make_person(admin_session, tenant_b)
    admin_session.commit()

    # Query as tenant_b's context; RLS should filter to tenant_b only
    app_user_session.execute(
        text("SELECT set_config('app.current_tenant', :t, true)"),
        {"t": str(tenant_b.id)},
    )
    items = notif_svc.recent(app_user_session, tenant_id=tenant_b.id, person_id=person_b2.id)
    titles = [n.title for n in items]
    assert "Tenant A secret notification" not in titles

    # Direct count for tenant_a in tenant_b session should also be 0
    count = notif_svc.unread_count(app_user_session, tenant_id=tenant_a.id, person_id=person_a.id)
    assert count == 0
