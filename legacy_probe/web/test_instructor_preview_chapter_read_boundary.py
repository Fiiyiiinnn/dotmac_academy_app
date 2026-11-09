"""Hosted-only canaries for the bounded preview chapter read delegation."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from dotmac_learning import ChapterFact
from fastapi import HTTPException

from app.web import instructor


class NoRouteDatabaseWork:
    def __getattr__(self, name):
        raise AssertionError(f"Unexpected route database operation: {name}")


@pytest.mark.parametrize(
    ("number", "empty", "selected", "missing"),
    [
        (None, False, 0, False),
        (7, False, 1, False),
        (None, True, None, False),
        (3, False, None, True),
        (7, True, None, True),
    ],
    ids=["first", "explicit", "empty-course", "missing-number", "empty-explicit"],
)
def test_preview_chapter_scope_selection_and_response_boundary(monkeypatch, number, empty, selected, missing):
    db = NoRouteDatabaseWork()
    request = object()
    tenant_id, person_id, requested_course_id, authorized_course_id = (uuid4() for _ in range(4))
    course = SimpleNamespace(id=authorized_course_id)
    facts = (
        ()
        if empty
        else (
            ChapterFact(uuid4(), tenant_id, course.id, 2, "First", "I", "Source", "<p>First</p>", "a", 9),
            ChapterFact(uuid4(), tenant_id, course.id, 7, "Second", "II", "Source", "<p>Second</p>", "b", 1),
        )
    )
    events = []
    rendered = []
    response = object()

    def authorize(actual_db, **scope):
        events.append(("authorize", actual_db, scope))
        return course

    def read(actual_db, **scope):
        events.append(("read", actual_db, scope))
        return facts

    def render(*args):
        rendered.append(args)
        return response

    monkeypatch.setattr(
        instructor, "require_tenant", lambda actual: SimpleNamespace(id=tenant_id) if actual is request else None
    )
    monkeypatch.setattr(instructor, "_authorable_course_or_404", authorize)
    monkeypatch.setattr(instructor, "authoring_chapters", read)
    monkeypatch.setattr(instructor, "_preview_course_response", render)
    person = SimpleNamespace(id=person_id)

    def invoke():
        if number is None:
            return instructor.preview_course(requested_course_id, request, person, db)
        return instructor.preview_course_chapter(requested_course_id, number, request, person, db)

    if missing:
        with pytest.raises(HTTPException) as observed:
            invoke()
        assert observed.value.status_code == 404
        assert rendered == []
    else:
        assert invoke() is response
        assert len(rendered) == 1
        actual_request, actual_db, actual_tenant, actual_course, actual_facts, actual_chapter = rendered[0]
        assert actual_request is request
        assert actual_db is db
        assert actual_tenant == tenant_id
        assert actual_course is course
        assert actual_facts is facts
        assert actual_chapter is (None if selected is None else facts[selected])
    assert events == [
        ("authorize", db, {"tenant_id": tenant_id, "person_id": person_id, "course_id": requested_course_id}),
        ("read", db, {"tenant_id": tenant_id, "course_id": authorized_course_id}),
    ]


@pytest.mark.parametrize("explicit", [False, True], ids=["course", "chapter"])
@pytest.mark.parametrize("status_code", [403, 404])
def test_preview_authorization_failure_precedes_owner_read(monkeypatch, explicit, status_code):
    db = NoRouteDatabaseWork()
    tenant_id, person_id, course_id = uuid4(), uuid4(), uuid4()
    error = HTTPException(status_code=status_code, detail="Existing authorization result")
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
        if explicit:
            instructor.preview_course_chapter(course_id, 7, object(), SimpleNamespace(id=person_id), db)
        else:
            instructor.preview_course(course_id, object(), SimpleNamespace(id=person_id), db)
    assert observed.value is error
    assert calls == [(db, {"tenant_id": tenant_id, "person_id": person_id, "course_id": course_id})]
