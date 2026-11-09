# tests/services/test_agenda.py
"""Academy agenda facade (app/services/agenda.py): labels, links, join fields and the cap source.

Pure: no database. The calendar domain itself (due/past/window selection, access scoping,
chronological order) is owned by dotmac-learning and covered by Starter
tests/test_learning_todo_agenda.py at the locked wheel; see docs/inventories/test_dispositions.toml.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import dotmac_learning as learning
import pytest

from app.services import agenda

NOW = datetime(2026, 10, 18, 12, tzinfo=UTC)


def _row(kind, *, title="Item", course_title="Course", target=None, session=None, joinable=False):
    return SimpleNamespace(
        when=NOW, kind=kind, title=title, course_title=course_title,
        target=target or SimpleNamespace(id=uuid4(), course_slug="slug"), session=session, joinable=joinable,
    )


@pytest.mark.parametrize("kind", ["opens", "closes"])
def test_window_items_get_kind_suffix_and_course_link(kind):
    item = agenda._item(_row(kind, title="Networking", target=SimpleNamespace(course_slug="net-101")))
    assert item == {
        "when": NOW, "kind": kind, "title": f"Networking {kind}", "course": "Course", "link": "/courses/net-101",
    }


def test_due_item_links_to_the_activity():
    target = SimpleNamespace(id=uuid4())
    item = agenda._item(_row("due", title="Quiz 1", target=target))
    assert item == {"when": NOW, "kind": "due", "title": "Quiz 1", "course": "Course", "link": f"/activities/{target.id}"}


def test_session_item_labels_location_link_and_join_fields():
    session = SimpleNamespace(
        session_type="lab", title="Lab", location="Room 4", join_url="https://join.example.test/x", id=uuid4()
    )
    item = agenda._item(_row("session", title="ignored", course_title="", target=SimpleNamespace(id=session.id),
                             session=session, joinable=True))
    assert item == {
        "when": NOW,
        "kind": "session",
        "title": "Lab (lab)",
        "course": "Room 4",
        "link": f"/timetable/sessions/{session.id}",
        "join_url": "https://join.example.test/x",
        "joinable": True,
    }


def test_live_class_session_has_no_type_suffix_and_blank_location_is_empty_string():
    session = SimpleNamespace(session_type="live_class", title="Seminar", location=None, join_url=None, id=uuid4())
    item = agenda._item(_row("session", target=SimpleNamespace(id=session.id), session=session))
    assert item["title"] == "Seminar"
    assert item["course"] == ""
    assert item["join_url"] is None
    assert item["joinable"] is False


def test_session_item_without_a_session_fact_is_refused():
    with pytest.raises(AssertionError):
        agenda._item(_row("session", session=None))


def _stub_owner(monkeypatch, db, policy):
    seen = []

    def effective(actual_db, *, require_store):
        assert actual_db is db
        assert require_store is True
        return policy

    def owner(actual_db, **values):
        assert actual_db is db
        seen.append(values)
        return ()

    monkeypatch.setattr(agenda, "effective", effective)
    monkeypatch.setattr(learning, "upcoming_agenda", owner)
    return seen


def test_omitted_limit_comes_from_settings_and_lead_minutes_are_forwarded(monkeypatch):
    db, tenant, person = object(), uuid4(), uuid4()
    seen = _stub_owner(monkeypatch, db, {"learning_agenda_limit": 7, "learning_join_lead_minutes": 9})
    assert agenda.upcoming_for_person(db, tenant_id=tenant, person_id=person) == []
    assert seen[0]["limit"] == 7
    assert seen[0]["join_lead_minutes"] == 9
    assert seen[0]["tenant_id"] == tenant and seen[0]["person_id"] == person


@pytest.mark.parametrize("explicit", [0, 3])
def test_explicit_limit_including_zero_is_kept(monkeypatch, explicit):
    db = object()
    seen = _stub_owner(monkeypatch, db, {"learning_agenda_limit": 7, "learning_join_lead_minutes": 9})
    agenda.upcoming_for_person(db, tenant_id=uuid4(), person_id=uuid4(), limit=explicit)
    assert seen[0]["limit"] == explicit
