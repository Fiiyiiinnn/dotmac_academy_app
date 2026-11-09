"""Tests for the audit viewer service helper (list_events)."""

from __future__ import annotations

from app.services.audit import list_events
from legacy_probe.services.test_canonical_audit_web_read import seed_historical_event as write_audit_event
from legacy_probe.services.test_canonical_audit_web_read import seed_person


def _seed_three(db, tenant_id, actor_id=None):
    """Seed three events with distinct actions; only e3 has a NULL actor."""
    e1 = write_audit_event(
        db,
        tenant_id=tenant_id,
        actor_person_id=actor_id,
        action="user.login",
        entity_type="person",
        entity_id=str(actor_id) if actor_id else None,
    )
    e2 = write_audit_event(
        db,
        tenant_id=tenant_id,
        actor_person_id=actor_id,
        action="course.viewed",
        entity_type="course",
        entity_id="course-1",
    )
    e3 = write_audit_event(
        db,
        tenant_id=tenant_id,
        actor_person_id=None,
        action="system.sweep",
        entity_type="system",
    )
    db.commit()
    return e1, e2, e3


def test_list_events_returns_newest_first(admin_session, tenant_a):
    _seed_three(admin_session, tenant_a.id)
    events = list_events(admin_session, tenant_id=tenant_a.id)
    assert len(events) >= 3
    times = [e.created_at for e in events]
    assert times == sorted(times, reverse=True)
    admin_session.rollback()


def test_list_events_action_filter(admin_session, tenant_a):
    _seed_three(admin_session, tenant_a.id)
    events = list_events(admin_session, tenant_id=tenant_a.id, action="user.login")
    assert len(events) >= 1
    assert all(e.action == "user.login" for e in events)
    admin_session.rollback()


def test_list_events_action_filter_excludes_others(admin_session, tenant_a):
    _seed_three(admin_session, tenant_a.id)
    events = list_events(admin_session, tenant_id=tenant_a.id, action="user.login")
    actions = {e.action for e in events}
    assert "course.viewed" not in actions
    assert "system.sweep" not in actions
    admin_session.rollback()


def test_list_events_actor_filter(admin_session, tenant_a):
    actor = seed_person(
        admin_session,
        tenant_id=tenant_a.id,
        email="actor@a.edu",
        first_name="Actor",
        last_name="Test",
    )
    admin_session.add(actor)
    admin_session.flush()
    _seed_three(admin_session, tenant_a.id, actor_id=actor.id)

    events = list_events(admin_session, tenant_id=tenant_a.id, actor_person_id=actor.id)
    assert len(events) >= 1
    assert all(e.actor_person_id == actor.id for e in events)
    admin_session.rollback()


def test_list_events_null_actor_included_without_filter(admin_session, tenant_a):
    _seed_three(admin_session, tenant_a.id)
    events = list_events(admin_session, tenant_id=tenant_a.id)
    null_actor_events = [e for e in events if e.actor_person_id is None]
    assert len(null_actor_events) >= 1


def test_list_events_limit_offset(admin_session, tenant_a):
    for i in range(5):
        write_audit_event(
            admin_session,
            tenant_id=tenant_a.id,
            actor_person_id=None,
            action=f"batch.{i}",
            entity_type="test",
        )
    admin_session.commit()

    page1 = list_events(admin_session, tenant_id=tenant_a.id, action=None, limit=3, offset=0)
    page2 = list_events(admin_session, tenant_id=tenant_a.id, action=None, limit=3, offset=3)
    assert len(page1) == 3
    # No overlap between pages.
    ids1 = {e.id for e in page1}
    ids2 = {e.id for e in page2}
    assert not ids1.intersection(ids2)
    admin_session.rollback()
