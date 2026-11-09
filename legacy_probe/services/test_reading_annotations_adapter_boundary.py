"""Hosted-only reading facade boundaries; no database or installed proof."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import dotmac_learning as learning
import pytest
from fastapi.responses import HTMLResponse
from jinja2 import ChoiceLoader, DictLoader

from app.services import bookmarks, reading_progress
from app.web import bookmarks as bookmarks_web
from app.web.templating import templates


def _facts():
    tenant_id, person_id, course_id, chapter_id = (uuid4() for _ in range(4))
    now = datetime(2026, 10, 17, tzinfo=UTC)
    course = learning.ReadingCourseFact(course_id, tenant_id, "course", "Same title")
    chapter = learning.ReadingChapterFact(chapter_id, tenant_id, course_id, 1, "First chapter")
    note = learning.NoteFact(uuid4(), tenant_id, person_id, course_id, chapter_id, "<b>private</b>", now, now)
    return tenant_id, person_id, course, chapter, note


def _render(name, **context):
    env = templates.env.overlay(
        loader=ChoiceLoader(
            [
                DictLoader({"base.html": "{% block content %}{% endblock %}"}),
                templates.env.loader,
            ]
        )
    )
    return env.get_template(name).render(**context)


@pytest.mark.parametrize("bookmarked", [False, True])
@pytest.mark.parametrize("has_note", [False, True])
def test_state_preserves_fact_leaf_and_nulls_in_existing_partials(monkeypatch, bookmarked, has_note):
    session = object()
    tenant_id, person_id, course, chapter, fact = _facts()
    note = fact if has_note else None
    calls = []

    def owner(db, **kw):
        assert db is session and kw == {
            "tenant_id": tenant_id,
            "person_id": person_id,
            "chapter_id": chapter.id,
        }
        calls.append(kw)
        return learning.AnnotationState(bookmarked, note)

    monkeypatch.setattr(learning, "annotation_state", owner)
    state = bookmarks.get_state(session, tenant_id=tenant_id, person_id=person_id, chapter_id=chapter.id)
    assert state == {"bookmarked": bookmarked, "note": note} and state["note"] is note
    assert len(calls) == 1
    html = _render(
        "bookmarks/_toggle.html",
        course=course,
        chapter=chapter,
        bookmarked=state["bookmarked"],
    )
    assert f'aria-pressed="{str(bookmarked).lower()}"' in html
    html = _render(
        "bookmarks/_note.html",
        course=course,
        chapter=chapter,
        note=state["note"],
        saved=False,
    )
    assert "/bookmarks/chapters/course/1/note" in html
    if has_note:
        assert "&lt;b&gt;private&lt;/b&gt;" in html and "<b>private</b>" not in html
    else:
        assert "private&lt;/b&gt;" not in html


@pytest.mark.parametrize("expected", [False, True])
def test_toggle_builds_actual_command_and_returns_owner_boolean(monkeypatch, expected):
    session = object()
    tenant_id, person_id, course, chapter, note = _facts()

    def owner(db, command):
        assert db is session and isinstance(command, learning.AnnotationCommand)
        assert command.model_dump() == {
            "tenant_id": tenant_id,
            "person_id": person_id,
            "course_id": course.id,
            "chapter_id": chapter.id,
        }
        return expected

    monkeypatch.setattr(learning, "toggle_bookmark", owner)
    assert (
        bookmarks.toggle_bookmark(
            session,
            tenant_id=tenant_id,
            person_id=person_id,
            course_id=course.id,
            chapter_id=chapter.id,
        )
        is expected
    )


@pytest.mark.parametrize(
    "raw, deleted",
    [(None, True), ("", True), ("  ", True), ("  <b>private</b>  ", False)],
)
def test_note_command_forwards_raw_body_and_returns_fact_or_none(monkeypatch, raw, deleted):
    session = object()
    tenant_id, person_id, course, chapter, note = _facts()
    result = None if deleted else note

    def owner(db, command):
        assert db is session and isinstance(command, learning.NoteCommand)
        assert (
            command.tenant_id,
            command.person_id,
            command.course_id,
            command.chapter_id,
        ) == (tenant_id, person_id, course.id, chapter.id)
        assert command.body == raw  # trimming/deletion is not a product copy
        return result

    monkeypatch.setattr(learning, "save_note", owner)
    assert (
        bookmarks.save_note(
            session,
            tenant_id=tenant_id,
            person_id=person_id,
            course_id=course.id,
            chapter_id=chapter.id,
            body=raw,
        )
        is result
    )


def test_listing_reads_visibility_once_then_preserves_group_order_and_fact_leaves(
    monkeypatch,
):
    session = object()
    tenant_id, person_id, course, chapter, note = _facts()
    other = learning.ReadingCourseFact(uuid4(), tenant_id, "other", course.title)
    other_chapter = learning.ReadingChapterFact(uuid4(), tenant_id, other.id, 4, "Other chapter")
    visible = {course.id, other.id}
    groups = (
        learning.AnnotationGroup(other, (learning.AnnotatedChapter(other_chapter, False, None),)),
        learning.AnnotationGroup(course, (learning.AnnotatedChapter(chapter, True, note),)),
    )
    calls = []

    def visibility(db, **kw):
        assert db is session and kw == {"tenant_id": tenant_id, "person_id": person_id}
        calls.append("visibility")
        return visible

    def owner(db, **kw):
        assert db is session
        assert kw["tenant_id"] == tenant_id and kw["person_id"] == person_id
        assert isinstance(kw["visible_course_ids"], tuple) and set(kw["visible_course_ids"]) == visible
        assert len(kw["visible_course_ids"]) == 2
        calls.append("annotations")
        return groups

    monkeypatch.setattr(bookmarks, "visible_course_ids", visibility)
    monkeypatch.setattr(learning, "list_annotations", owner)
    result = bookmarks.list_for_person(session, tenant_id=tenant_id, person_id=person_id)
    assert calls == ["visibility", "annotations"]
    assert isinstance(result, list) and isinstance(result[0]["chapters"], list)
    assert result[0]["course"] is other and result[1]["course"] is course
    assert result[0]["chapters"][0] == {
        "chapter": other_chapter,
        "bookmarked": False,
        "note": None,
    }
    assert result[1]["chapters"][0]["chapter"] is chapter and result[1]["chapters"][0]["note"] is note
    html = _render("bookmarks/index.html", groups=result)
    assert html.index("Other chapter") < html.index("First chapter")
    assert "/courses/other/chapters/4" in html and "/courses/course/chapters/1" in html
    assert "&lt;b&gt;private&lt;/b&gt;" in html and "<b>private</b>" not in html


def test_no_visibility_retains_early_empty_listing(monkeypatch):
    session = object()
    tenant_id, person_id, course, chapter, note = _facts()
    monkeypatch.setattr(bookmarks, "visible_course_ids", lambda db, **kw: set())
    monkeypatch.setattr(
        learning,
        "list_annotations",
        lambda *a, **kw: pytest.fail("listing ran without visible courses"),
    )
    result = bookmarks.list_for_person(session, tenant_id=tenant_id, person_id=person_id)
    assert result == [] and "Nothing saved yet." in _render("bookmarks/index.html", groups=result)


@pytest.mark.parametrize("empty", [False, True])
def test_progress_read_containers_and_chapter_input_are_not_rebuilt(monkeypatch, empty):
    session = object()
    tenant_id, person_id, course, chapter, note = _facts()
    chapter_ids = [] if empty else [chapter.id, chapter.id, uuid4()]
    slugs = set() if empty else {"intro", "what-next"}
    grouped = {} if empty else {chapter.id: slugs}

    def single(db, **kw):
        assert db is session and kw == {
            "tenant_id": tenant_id,
            "person_id": person_id,
            "chapter_id": chapter.id,
        }
        return slugs

    def bulk(db, **kw):
        assert db is session and kw["tenant_id"] == tenant_id and kw["person_id"] == person_id
        assert kw["chapter_ids"] is chapter_ids
        return grouped

    monkeypatch.setattr(learning, "completed_slugs", single)
    monkeypatch.setattr(learning, "completed_slugs_by_chapter", bulk)
    assert (
        reading_progress.completed_slugs(session, tenant_id=tenant_id, person_id=person_id, chapter_id=chapter.id)
        is slugs
    )
    result = reading_progress.completed_slugs_by_chapter(
        session, tenant_id=tenant_id, person_id=person_id, chapter_ids=chapter_ids
    )
    assert result is grouped
    if not empty:
        assert result[chapter.id] is slugs


@pytest.mark.parametrize(
    "raw, created",
    [(None, False), ("   ", False), ("  intro  ", True), ("x" * 80, False)],
)
def test_subtopic_command_keeps_raw_slug_and_public_boolean(monkeypatch, raw, created):
    session = object()
    tenant_id, person_id, course, chapter, note = _facts()

    def owner(db, command):
        assert db is session and isinstance(command, learning.SubtopicReadCommand)
        assert (command.tenant_id, command.person_id, command.chapter_id) == (
            tenant_id,
            person_id,
            chapter.id,
        )
        assert command.subtopic_slug == raw  # no local trim/cap/idempotency decision
        return created

    monkeypatch.setattr(learning, "mark_subtopic_complete", owner)
    assert (
        reading_progress.mark_complete(
            session,
            tenant_id=tenant_id,
            person_id=person_id,
            chapter_id=chapter.id,
            subtopic_slug=raw,
        )
        is created
    )


@pytest.mark.parametrize("deleted", [False, True])
def test_actual_note_save_caller_renders_detached_fact_or_blank(monkeypatch, deleted):
    session = object()
    tenant_id, person_id, course, chapter, note = _facts()
    request = object()
    result = None if deleted else note
    monkeypatch.setattr(bookmarks_web, "require_tenant", lambda request: SimpleNamespace(id=tenant_id))
    monkeypatch.setattr(bookmarks_web, "_resolve", lambda *a: (course, chapter))

    def owner(db, command):
        assert db is session and isinstance(command, learning.NoteCommand)
        assert command.body == ("  " if deleted else "  <b>private</b>  ")
        return result

    def render(request, name, context):
        assert name == "bookmarks/_note.html" and context["note"] is result and context["saved"] is True
        return HTMLResponse(_render(name, **context))

    monkeypatch.setattr(learning, "save_note", owner)
    monkeypatch.setattr(bookmarks_web.templates, "TemplateResponse", render)
    response = bookmarks_web.note_save(
        course.slug,
        chapter.number,
        request,
        body="  " if deleted else "  <b>private</b>  ",
        person=SimpleNamespace(id=person_id),
        db=session,
    )
    assert response.status_code == 200
    assert (b"&lt;b&gt;private&lt;/b&gt;" in response.body) is (not deleted)


@pytest.mark.parametrize("operation", ["toggle", "note", "subtopic"])
def test_required_public_command_errors_escape_unchanged(monkeypatch, operation):
    session = object()
    tenant_id, person_id, course, chapter, note = _facts()
    failure = learning.TransactionChanged("caller transaction changed")

    def refused(*a, **kw):
        raise failure

    targets = {
        "toggle": (
            bookmarks.toggle_bookmark,
            "toggle_bookmark",
            {"course_id": course.id},
        ),
        "note": (
            bookmarks.save_note,
            "save_note",
            {"course_id": course.id, "body": "text"},
        ),
        "subtopic": (
            reading_progress.mark_complete,
            "mark_subtopic_complete",
            {"subtopic_slug": "intro"},
        ),
    }
    function, public_name, arguments = targets[operation]
    monkeypatch.setattr(learning, public_name, refused)
    with pytest.raises(learning.TransactionChanged) as raised:
        function(
            session,
            tenant_id=tenant_id,
            person_id=person_id,
            chapter_id=chapter.id,
            **arguments,
        )
    assert raised.value is failure
