"""RLS isolation tests for announcements.

Tenant A's announcements must not be visible when querying as Tenant B
through app_user (RLS enforced).
"""
from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.services import announcements as ann_svc
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

def _make_person(db, tenant):
    pid = uuid4()
    p = make_person_party(db, tenant_id=tenant.id, email=f"{pid}@iso.ann", first_name="I", last_name="S")
    pid = p.id
    db.add(p)
    db.flush()
    return p

def test_tenant_b_cannot_see_tenant_a_announcement(
    admin_session, app_user_session, tenant_a, tenant_b
):
    """Querying as tenant_b via app_user cannot see tenant_a announcements."""
    _admin_set_tenant(admin_session, tenant_a.id)
    author_a = _make_person(admin_session, tenant_a)

    ann = ann_svc.create(
        admin_session,
        tenant_id=tenant_a.id,
        author_person_id=author_a.id,
        title="Secret for tenant A",
        body_md="Top secret",
    )
    admin_session.commit()

    _admin_set_tenant(admin_session, tenant_b.id)
    person_b = _make_person(admin_session, tenant_b)
    admin_session.commit()

    # Query as tenant_b via app_user (RLS active)
    app_user_session.execute(
        text("SELECT set_config('app.current_tenant', :t, true)"),
        {"t": str(tenant_b.id)},
    )
    items = ann_svc.list_for_tenant(app_user_session, tenant_id=tenant_b.id)
    titles = [a.title for a in items]
    assert "Secret for tenant A" not in titles

    # for_person as tenant_b also must not see tenant_a's announcement
    items_fp = ann_svc.for_person(
        app_user_session, tenant_id=tenant_b.id, person_id=person_b.id
    )
    assert not any(a.id == ann.id for a in items_fp)

def test_tenant_a_announcement_visible_within_tenant_a(
    admin_session, app_user_session, tenant_a
):
    """Within tenant_a, the announcement is visible via app_user."""
    _admin_set_tenant(admin_session, tenant_a.id)
    author = _make_person(admin_session, tenant_a)

    ann = ann_svc.create(
        admin_session,
        tenant_id=tenant_a.id,
        author_person_id=author.id,
        title="Visible in tenant A",
        body_md="Hello A",
    )
    admin_session.commit()

    # Query as tenant_a via app_user
    app_user_session.execute(
        text("SELECT set_config('app.current_tenant', :t, true)"),
        {"t": str(tenant_a.id)},
    )
    items = ann_svc.list_for_tenant(app_user_session, tenant_id=tenant_a.id)
    ids = [a.id for a in items]
    assert ann.id in ids
