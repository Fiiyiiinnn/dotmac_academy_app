"""Product projections and strict configured delegation; hosted execution only.

These boundary canaries do not substitute for installed canonical PostgreSQL
fixtures, identity integration or retirement of the local audited update writer.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import dotmac_learning as learning
import pytest

from app.services import agenda, announcements, at_risk, scheduling, search, todo

NOW = datetime(2026, 10, 18, 12, tzinfo=UTC)


def _strict(monkeypatch, module, db, values):
    calls = []

    def effective(actual_db, *, require_store):
        assert actual_db is db
        assert require_store is True
        calls.append(values)
        return values

    monkeypatch.setattr(module, "effective", effective)
    return calls


def test_timeline_preserves_product_containers_and_passes_terms_zone_clock(monkeypatch):
    db, tenant, person = object(), uuid4(), uuid4()
    terms = learning.TodoTerms(3, 2, 11, 2, 5)
    activity = SimpleNamespace(id=uuid4(), title="Task")
    deadline = SimpleNamespace(due_at=NOW, activity=activity, course_title="Course", course_slug="course")
    grade = SimpleNamespace(
        score_id=uuid4(),
        fraction=0.4,
        passed=False,
        activity=activity,
        course_title="Course",
        course_slug="course",
        feedback_available=True,
    )
    session = SimpleNamespace(id=uuid4())
    owner = SimpleNamespace(
        overdue=(deadline,),
        due_today=(),
        next_7=(deadline,),
        next_30=(),
        sessions=(SimpleNamespace(session=session, cohort_name="Cohort"),),
        joinable=frozenset({session.id}),
        graded=(grade,),
        feedback=(grade,),
        course_windows=(SimpleNamespace(kind="opens", at=NOW, course_title="Course"),),
    )
    _strict(monkeypatch, todo, db, {"learning_join_lead_minutes": 4})
    monkeypatch.setattr(
        todo, "todo_terms", lambda actual_db: terms if actual_db is db else pytest.fail("Session changed")
    )
    monkeypatch.setattr(todo, "academy_zone", lambda: UTC)
    feedback_terms = learning.FeedbackRevealTerms(
        reveal_by_mode={"practice": "immediate", "graded": "on_pass_or_exhausted", "exam": "never"}
    )
    monkeypatch.setattr(
        todo,
        "feedback_reveal_terms",
        lambda actual_db: feedback_terms if actual_db is db else pytest.fail("Session changed"),
    )

    def timeline(actual_db, **values):
        assert actual_db is db
        assert values == {
            "tenant_id": tenant,
            "person_id": person,
            "now": NOW,
            "local_zone": UTC,
            "terms": terms,
            "feedback_terms": feedback_terms,
            "join_lead_minutes": 4,
        }
        return owner

    monkeypatch.setattr(learning, "timeline_for_person", timeline)
    result = todo.timeline_for_person(db, tenant_id=tenant, person_id=person, now=NOW)
    assert result["overdue"] == [
        {"due_at": NOW, "activity": activity, "course_title": "Course", "course_slug": "course"}
    ]
    assert result["sessions"] == [(session, "Cohort")]
    assert result["near_deadline_days"] == 2
    assert result["far_deadline_days"] == 5
    assert result["window_horizon_days"] == 11
    assert result["joinable"] == {session.id}
    assert isinstance(result["joinable"], set)
    assert result["graded"][0]["score"] is grade
    assert result["graded"][0]["score"].fraction == 0.4
    assert result["feedback"][0] is result["graded"][0]
    assert result["course_windows"] == [{"kind": "opens", "at": NOW, "course_title": "Course"}]


def test_agenda_preserves_labels_links_locations_join_fields_and_owner_order(monkeypatch):
    db, tenant, person = object(), uuid4(), uuid4()
    _strict(monkeypatch, agenda, db, {"learning_agenda_limit": 2, "learning_join_lead_minutes": 4})
    session = SimpleNamespace(
        session_type="lab", title="Lab", location="Room", join_url="https://example.test", id=uuid4()
    )
    rows = (
        SimpleNamespace(
            when=NOW,
            kind="session",
            title="Lab",
            course_title="",
            target=SimpleNamespace(id=session.id),
            session=session,
            joinable=True,
        ),
        SimpleNamespace(
            when=NOW,
            kind="opens",
            title="Course",
            course_title="Course",
            target=SimpleNamespace(course_slug="course"),
            session=None,
            joinable=False,
        ),
    )

    def owner(actual_db, **values):
        assert actual_db is db
        assert values["tenant_id"] == tenant and values["person_id"] == person
        assert values["limit"] == 2 and values["join_lead_minutes"] == 4
        return rows

    monkeypatch.setattr(learning, "upcoming_agenda", owner)
    result = agenda.upcoming_for_person(db, tenant_id=tenant, person_id=person)
    assert result == [
        {
            "when": NOW,
            "kind": "session",
            "title": "Lab (lab)",
            "course": "Room",
            "link": f"/timetable/sessions/{session.id}",
            "join_url": session.join_url,
            "joinable": True,
        },
        {"when": NOW, "kind": "opens", "title": "Course opens", "course": "Course", "link": "/courses/course"},
    ]


@pytest.mark.parametrize(
    "module,function,key,public,extra",
    [
        (
            announcements,
            "for_person",
            "learning_person_announcements_limit",
            "announcements_for_person",
            {"person_id": uuid4()},
        ),
        (announcements, "list_for_tenant", "learning_tenant_announcements_limit", "list_announcements", {}),
    ],
)
def test_announcement_caps_resolve_strictly_and_preserve_explicit_zero(
    monkeypatch, module, function, key, public, extra
):
    db, tenant = object(), uuid4()
    calls = _strict(monkeypatch, module, db, {key: 3})
    values = []
    row = object()

    def owner(actual_db, **kwargs):
        assert actual_db is db
        values.append(kwargs)
        return (row,)

    monkeypatch.setattr(learning, public, owner)
    assert getattr(module, function)(db, tenant_id=tenant, **extra) == [row]
    assert values[-1] == {"tenant_id": tenant, **extra, "limit": 3}
    assert getattr(module, function)(db, tenant_id=tenant, limit=0, **extra) == [row]
    assert values[-1]["limit"] == 0
    assert len(calls) == 1


def test_search_consumes_configured_cap_without_changing_presentation(monkeypatch):
    db, tenant, person = object(), uuid4(), uuid4()
    _strict(monkeypatch, search, db, {"learning_search_limit": 1})

    def owner(actual_db, **values):
        assert actual_db is db
        assert values["limit"] == 1
        assert values["authority"] == learning.SearchAuthority(tenant, person, False)
        return SimpleNamespace(
            term="needle",
            courses=(SimpleNamespace(slug="c", title="Course"),),
            chapters=(
                SimpleNamespace(
                    course_slug="c", chapter=SimpleNamespace(title="Chapter", number=2, body_html="<p>needle here</p>")
                ),
            ),
        )

    monkeypatch.setattr(search, "search_learning", owner)
    assert search.search(db, tenant_id=tenant, person_id=person, q="needle", is_staff=False) == {
        "courses": [{"slug": "c", "title": "Course"}],
        "chapters": [{"title": "Chapter", "course_slug": "c", "chapter_number": 2, "snippet": "needle here"}],
    }


def test_at_risk_consumes_configured_threshold_and_preserves_empty_title(monkeypatch):
    db, tenant, person, course = object(), uuid4(), uuid4(), uuid4()
    _strict(monkeypatch, at_risk, db, {"learning_behind_pace_fraction": 0.4})

    def owner(actual_db, **values):
        assert actual_db is db
        assert values == {"tenant_id": tenant, "person_id": person, "behind_threshold": 0.4, "now": NOW}
        return (SimpleNamespace(course_id=course, title="", reason="behind", pct=0.1, expected=0.6),)

    monkeypatch.setattr(learning, "at_risk_for_person", owner)
    assert at_risk.at_risk_for_person(db, tenant_id=tenant, person_id=person, now=NOW) == [
        {"course_id": course, "title": "", "reason": "behind", "pct": 0.1, "expected": 0.6}
    ]


@pytest.mark.parametrize(
    "module,function,extra",
    [
        (agenda, "upcoming_for_person", {"person_id": uuid4()}),
        (announcements, "for_person", {"person_id": uuid4()}),
        (announcements, "list_for_tenant", {}),
        (at_risk, "at_risk_for_person", {"person_id": uuid4()}),
        (search, "search", {"person_id": uuid4(), "q": "needle", "is_staff": False}),
    ],
)
def test_missing_policy_has_no_hidden_fallback(monkeypatch, module, function, extra):
    db = object()
    _strict(monkeypatch, module, db, {})
    with pytest.raises(KeyError):
        getattr(module, function)(db, tenant_id=uuid4(), **extra)


def test_scheduling_person_containers_owner_order_and_explicit_join_input(monkeypatch):
    db, tenant, person, session_id = object(), uuid4(), uuid4(), uuid4()
    first, second = object(), object()
    entry = SimpleNamespace(session=first, cohort_name="First")

    def timetable(actual_db, **values):
        assert actual_db is db
        assert values == {"tenant_id": tenant, "person_id": person, "now": NOW}
        return SimpleNamespace(upcoming=(entry,), past=(SimpleNamespace(session=second, cohort_name="Second"),))

    def single(actual_db, **values):
        assert actual_db is db
        assert values == {"tenant_id": tenant, "person_id": person, "session_id": session_id}
        return entry

    def join(session, **values):
        assert session is first
        assert values == {"now": NOW, "lead_minutes": 4}
        return True

    monkeypatch.setattr(learning, "person_timetable", timetable)
    monkeypatch.setattr(learning, "get_session_for_person", single)
    monkeypatch.setattr(learning, "join_is_open", join)
    assert scheduling.list_for_person(db, tenant_id=tenant, person_id=person, now=NOW) == {
        "upcoming": [(first, "First")],
        "past": [(second, "Second")],
    }
    assert scheduling.get_session_for_person(db, tenant_id=tenant, person_id=person, session_id=session_id) == (
        first,
        "First",
    )
    assert scheduling.join_is_open(first, now=NOW, lead_minutes=4)
    with pytest.raises(TypeError):
        scheduling.join_is_open(first, now=NOW)


def test_create_session_resolves_omitted_type_and_preserves_explicit_type(monkeypatch):
    db, tenant, cohort = object(), uuid4(), uuid4()
    calls = _strict(monkeypatch, scheduling, db, {"learning_default_session_type": "lab"})
    received = []
    result = object()

    def create(actual_db, command):
        assert actual_db is db
        received.append(command)
        return result

    monkeypatch.setattr(learning, "create_session", create)
    # The delivery-mode promotion flag is composed creation-default policy read on the same session.
    monkeypatch.setattr(
        scheduling,
        "creation_defaults",
        lambda actual_db: (
            SimpleNamespace(session_promotes_delivery_mode=True) if actual_db is db else pytest.fail("Session changed")
        ),
    )
    values = {"tenant_id": tenant, "cohort_id": cohort, "title": "Class", "starts_at": NOW}
    assert scheduling.create_session(db, **values) is result
    assert received[-1].session_type == "lab"
    assert received[-1].promote_delivery_mode is True
    assert scheduling.create_session(db, **values, session_type="lecture") is result
    assert received[-1].session_type == "lecture"
    assert len(calls) == 1


def test_reminder_deadline_seam_keeps_raw_values_and_same_session(monkeypatch):
    db, tenant, person = object(), uuid4(), uuid4()
    activity = object()
    row = SimpleNamespace(due_at=NOW, activity=activity, course_title="Course", course_slug="course")

    def deadlines(actual_db, **values):
        assert actual_db is db
        assert values == {"tenant_id": tenant, "person_id": person}
        return (row,)

    monkeypatch.setattr(learning, "deadline_rows", deadlines)
    assert todo._deadline_rows(db, tenant_id=tenant, person_id=person) == [
        {"due_at": NOW, "activity": activity, "course_title": "Course", "course_slug": "course"}
    ]


def test_todo_and_create_refuse_missing_configured_policy_before_owner(monkeypatch):
    db = object()
    _strict(monkeypatch, todo, db, {})
    _strict(monkeypatch, scheduling, db, {})
    monkeypatch.setattr(todo, "todo_terms", lambda actual_db: learning.TodoTerms(3, 2, 11, 2, 5))
    monkeypatch.setattr(
        todo,
        "feedback_reveal_terms",
        lambda actual_db: learning.FeedbackRevealTerms(
            reveal_by_mode={"practice": "immediate", "graded": "on_pass_or_exhausted", "exam": "never"}
        ),
    )

    def forbidden(*args, **kwargs):
        pytest.fail("missing policy must refuse before owner reads or writes")

    monkeypatch.setattr(learning, "timeline_for_person", forbidden)
    monkeypatch.setattr(learning, "create_session", forbidden)
    with pytest.raises(KeyError):
        todo.timeline_for_person(db, tenant_id=uuid4(), person_id=uuid4(), now=NOW)
    with pytest.raises(KeyError):
        scheduling.create_session(db, tenant_id=uuid4(), cohort_id=uuid4(), title="Class", starts_at=NOW)


def test_owner_unknown_errors_propagate_without_product_recovery(monkeypatch):
    db = object()
    error = RuntimeError("owner failure")
    _strict(monkeypatch, at_risk, db, {"learning_behind_pace_fraction": 0.4})

    def owner(*args, **kwargs):
        raise error

    monkeypatch.setattr(learning, "at_risk_for_person", owner)
    with pytest.raises(RuntimeError) as caught:
        at_risk.at_risk_for_person(db, tenant_id=uuid4(), person_id=uuid4())
    assert caught.value is error


def test_explicit_zero_agenda_cap_and_risk_threshold_are_not_replaced(monkeypatch):
    db = object()
    calls = _strict(monkeypatch, agenda, db, {"learning_join_lead_minutes": 4})
    received = []

    def owner(actual_db, **values):
        assert actual_db is db
        received.append(values)
        return ()

    monkeypatch.setattr(learning, "upcoming_agenda", owner)
    monkeypatch.setattr(learning, "at_risk_for_person", owner)
    assert agenda.upcoming_for_person(db, tenant_id=uuid4(), person_id=uuid4(), limit=0) == []
    assert received[-1]["limit"] == 0
    assert len(calls) == 1
    assert at_risk.at_risk_for_person(db, tenant_id=uuid4(), person_id=uuid4(), behind_threshold=0.0) == []
    assert received[-1]["behind_threshold"] == 0.0


def test_at_risk_missing_course_title_remains_product_copy(monkeypatch):
    row = SimpleNamespace(course_id=uuid4(), title=None, reason="overdue", pct=0, expected=1)
    monkeypatch.setattr(learning, "at_risk_for_person", lambda *args, **kwargs: (row,))
    result = at_risk.at_risk_for_person(object(), tenant_id=uuid4(), person_id=uuid4(), behind_threshold=0.4, now=NOW)
    assert result[0]["title"] == "your course"
