"""Service-level tests for app/services/notifications.py."""
from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text

from app.models.notification import Notification
from app.services import notifications as notif_svc
from legacy_probe.conftest import make_person_party


def _set_tenant(db, tenant_id):
    db.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(tenant_id)})

@pytest.fixture
def db_session(admin_session):
    """Admin session used for service tests (RLS bypassed)."""
    return admin_session

def test_notify_creates_row(db_session, tenant_a):
    _set_tenant(db_session, tenant_a.id)
    person_id = uuid4()

    p = make_person_party(db_session, tenant_id=tenant_a.id, email=f"{person_id}@test.com", first_name="A", last_name="B")
    person_id = p.id
    db_session.add(p)
    db_session.flush()

    n = notif_svc.notify(
        db_session,
        tenant_id=tenant_a.id,
        person_id=person_id,
        kind="result",
        title="Test notification",
        body="body text",
        link="/courses/test",
    )
    db_session.flush()

    assert isinstance(n, Notification)
    assert n.id is not None
    assert n.tenant_id == tenant_a.id
    assert n.person_id == person_id
    assert n.kind == "result"
    assert n.title == "Test notification"
    assert n.body == "body text"
    assert n.link == "/courses/test"
    assert n.read_at is None

def test_unread_count(db_session, tenant_a):
    _set_tenant(db_session, tenant_a.id)
    person_id = uuid4()

    p = make_person_party(db_session, tenant_id=tenant_a.id, email=f"{person_id}@test.com", first_name="C", last_name="D")
    person_id = p.id
    db_session.add(p)
    db_session.flush()

    assert notif_svc.unread_count(db_session, tenant_id=tenant_a.id, person_id=person_id) == 0

    notif_svc.notify(db_session, tenant_id=tenant_a.id, person_id=person_id,
                     kind="result", title="N1")
    notif_svc.notify(db_session, tenant_id=tenant_a.id, person_id=person_id,
                     kind="result", title="N2")
    db_session.flush()

    assert notif_svc.unread_count(db_session, tenant_id=tenant_a.id, person_id=person_id) == 2

def test_mark_all_read_zeroes_count(db_session, tenant_a):
    _set_tenant(db_session, tenant_a.id)
    person_id = uuid4()

    p = make_person_party(db_session, tenant_id=tenant_a.id, email=f"{person_id}@test.com", first_name="E", last_name="F")
    person_id = p.id
    db_session.add(p)
    db_session.flush()

    notif_svc.notify(db_session, tenant_id=tenant_a.id, person_id=person_id,
                     kind="result", title="N3")
    notif_svc.notify(db_session, tenant_id=tenant_a.id, person_id=person_id,
                     kind="certificate", title="N4")
    db_session.flush()

    assert notif_svc.unread_count(db_session, tenant_id=tenant_a.id, person_id=person_id) == 2

    notif_svc.mark_all_read(db_session, tenant_id=tenant_a.id, person_id=person_id)
    db_session.flush()

    assert notif_svc.unread_count(db_session, tenant_id=tenant_a.id, person_id=person_id) == 0

def test_recent_ordering(db_session, tenant_a):
    """recent() returns newest first."""
    from datetime import UTC, datetime

    _set_tenant(db_session, tenant_a.id)
    person_id = uuid4()

    p = make_person_party(db_session, tenant_id=tenant_a.id, email=f"{person_id}@test.com", first_name="G", last_name="H")
    person_id = p.id
    db_session.add(p)
    db_session.flush()

    n1 = notif_svc.notify(db_session, tenant_id=tenant_a.id, person_id=person_id,
                          kind="result", title="First")
    # server_default uses transaction timestamp; set explicit created_at to control order
    n1.created_at = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)

    n2 = notif_svc.notify(db_session, tenant_id=tenant_a.id, person_id=person_id,
                          kind="certificate", title="Second")
    n2.created_at = datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC)

    db_session.flush()

    items = notif_svc.recent(db_session, tenant_id=tenant_a.id, person_id=person_id)
    assert len(items) >= 2
    titles = [n.title for n in items]
    assert titles.index("Second") < titles.index("First")

def test_notify_many(db_session, tenant_a):
    _set_tenant(db_session, tenant_a.id)

    ids = []
    for _ in range(2):
        p = make_person_party(db_session, tenant_id=tenant_a.id, email=f"{uuid4()}@test.com", first_name="X", last_name="Y")
        ids.append(p.id)
        db_session.add(p)
    db_session.flush()

    notif_svc.notify_many(db_session, tenant_id=tenant_a.id, person_ids=ids,
                          kind="announcement", title="Hello everyone")
    db_session.flush()

    for pid in ids:
        assert notif_svc.unread_count(db_session, tenant_id=tenant_a.id, person_id=pid) == 1
