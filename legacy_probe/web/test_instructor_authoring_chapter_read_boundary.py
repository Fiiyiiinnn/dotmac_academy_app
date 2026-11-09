"""Hosted-only route canaries for the candidate Learning chapter read boundary."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from dotmac_learning import ChapterFact
from fastapi import HTTPException

from app.web import instructor


class NoLocalDatabaseWork:
    """The route must leave chapter queries and transaction control to their owners."""

    def __getattr__(self, name):
        raise AssertionError(f"Unexpected route database operation: {name}")


@pytest.mark.parametrize("empty", [False, True], ids=["facts", "empty"])
def test_authoring_chapters_preserve_scope_facts_editor_and_template(monkeypatch, empty):
    db = NoLocalDatabaseWork()
    tenant = SimpleNamespace(id=uuid4())
    person = SimpleNamespace(id=uuid4())
    course_id = uuid4()
    course = object()
    request = object()
    events = []
    # Deliberately retain owner order even when order_index suggests the reverse.
    facts = (
        ()
        if empty
        else (
            ChapterFact(uuid4(), tenant.id, course_id, 2, "First", "I", "# Stored source", "<p>Other</p>", "a", 9),
            ChapterFact(uuid4(), tenant.id, course_id, 7, "Second", "II", " ", "<p>Legacy text</p>", "b", 1),
        )
    )

    def authorize(actual_db, **scope):
        events.append(("authorize", actual_db, scope))
        return course

    def read(actual_db, **scope):
        events.append(("read", actual_db, scope))
        return facts

    response = object()
    rendered = []

    def render(*args):
        rendered.append(args)
        return response

    monkeypatch.setattr(instructor, "require_tenant", lambda actual: tenant if actual is request else None)
    monkeypatch.setattr(instructor, "_authorable_course_or_404", authorize)
    monkeypatch.setattr(instructor, "authoring_chapters", read)
    monkeypatch.setattr(instructor, "templates", SimpleNamespace(TemplateResponse=render))

    assert instructor.author_course_page(course_id, request, person, db) is response
    assert events == [
        ("authorize", db, {"tenant_id": tenant.id, "person_id": person.id, "course_id": course_id}),
        ("read", db, {"tenant_id": tenant.id, "course_id": course_id}),
    ]
    expected_rows = (
        []
        if empty
        else [
            {"chapter": facts[0], "source": "# Stored source"},
            {"chapter": facts[1], "source": "Legacy text"},
        ]
    )
    assert rendered == [
        (request, "instructor/authoring.html", {"request": request, "course": course, "chapters": expected_rows})
    ]
    if not empty:
        rows = rendered[0][2]["chapters"]
        assert all(row["chapter"] is fact for row, fact in zip(rows, facts, strict=True))
        assert [
            (row["chapter"].id, row["chapter"].number, row["chapter"].title, row["chapter"].part) for row in rows
        ] == [(facts[0].id, 2, "First", "I"), (facts[1].id, 7, "Second", "II")]


@pytest.mark.parametrize("status_code", [403, 404])
def test_authoring_authorization_failure_precedes_chapter_read(monkeypatch, status_code):
    error = HTTPException(status_code=status_code, detail="Existing authorization result")
    tenant_id, person_id, course_id = uuid4(), uuid4(), uuid4()
    db = NoLocalDatabaseWork()
    calls = []

    def authorize(actual_db, **scope):
        calls.append((actual_db, scope))
        raise error

    def forbidden_read(*args, **kwargs):
        raise AssertionError("Chapter read must not run after denied authorization")

    monkeypatch.setattr(instructor, "require_tenant", lambda request: SimpleNamespace(id=tenant_id))
    monkeypatch.setattr(instructor, "_authorable_course_or_404", authorize)
    monkeypatch.setattr(instructor, "authoring_chapters", forbidden_read)
    with pytest.raises(HTTPException) as observed:
        instructor.author_course_page(course_id, object(), SimpleNamespace(id=person_id), db)
    assert observed.value is error
    assert calls == [(db, {"tenant_id": tenant_id, "person_id": person_id, "course_id": course_id})]


def test_authoring_owner_failure_propagates_without_template_or_transaction_work(monkeypatch):
    error = RuntimeError("Learning read unavailable")
    db = NoLocalDatabaseWork()

    def read(*args, **kwargs):
        raise error

    def forbidden_render(*args, **kwargs):
        raise AssertionError("Failed reads must not render success")

    monkeypatch.setattr(instructor, "require_tenant", lambda request: SimpleNamespace(id=uuid4()))
    monkeypatch.setattr(instructor, "_authorable_course_or_404", lambda *args, **kwargs: object())
    monkeypatch.setattr(instructor, "authoring_chapters", read)
    monkeypatch.setattr(instructor, "templates", SimpleNamespace(TemplateResponse=forbidden_render))
    with pytest.raises(RuntimeError) as observed:
        instructor.author_course_page(uuid4(), object(), SimpleNamespace(id=uuid4()), db)
    assert observed.value is error
