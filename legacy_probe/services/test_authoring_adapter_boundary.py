"""Hosted-only authoring facade, product renderer and detached fact boundaries."""

from datetime import UTC, datetime
from uuid import uuid4

import dotmac_learning as learning
import pytest

from app.services import authoring
from app.services.exceptions import ConflictError, NotFoundError


def _course(tenant_id):
    now = datetime(2026, 10, 17, tzinfo=UTC)
    return learning.CourseFact(
        uuid4(),
        tenant_id,
        "mixed",
        "Résumé",
        "network",
        "in-app",
        1,
        "draft",
        False,
        now,
        now,
    )


def _chapter(tenant_id, course_id, body_md="", body_html="<p>Stored</p>"):
    return learning.ChapterFact(uuid4(), tenant_id, course_id, 2, "Chapter", "Part I", body_md, body_html, "", 2)


def test_course_command_preserves_raw_input_and_returns_actual_public_fact(monkeypatch):
    session = object()
    tenant_id = uuid4()
    fact = _course(tenant_id)
    calls = []

    def owner(db, command):
        assert db is session and isinstance(command, learning.CourseCreate)
        assert command.model_dump() == {
            "tenant_id": tenant_id,
            "slug": "  MiXeD  ",
            "title": "Résumé",
            "discipline": "network",
            "listed": False,
            "completion_fraction": 1.0,
            "certificate_auto_issue": True,
            "certificate_validity_days": None,
            "prerequisite_composition": "all",
        }
        calls.append(command)
        return fact

    monkeypatch.setattr(learning, "create_draft_course", owner)
    result = authoring.create_course(
        session,
        tenant_id=tenant_id,
        slug="  MiXeD  ",
        title="Résumé",
        discipline="network",
    )
    assert result is fact and result.id == fact.id and result.status == "draft"
    assert len(calls) == 1


@pytest.mark.parametrize("part", ["", "Part I"])
def test_chapter_command_passes_actual_renderer_once_and_never_skips_equal_save(monkeypatch, part):
    session = object()
    tenant_id, course_id = uuid4(), uuid4()
    raw = "# Heading\n\n**safe** <script>bad()</script>"
    fact = _chapter(tenant_id, course_id, raw)
    events = []
    real_renderer = authoring.render_markdown

    def renderer(body):
        assert body == raw
        events.append("render")
        return real_renderer(body)

    def owner(db, command, *, render_markdown):
        assert db is session and isinstance(command, learning.AuthorChapterCommand)
        assert command.model_dump() == {
            "tenant_id": tenant_id,
            "course_id": course_id,
            "number": 2,
            "title": "Chapter",
            "body_md": raw,
            "part": part,
        }
        assert render_markdown is authoring.render_markdown
        events.append("owner-admitted")
        html = render_markdown(command.body_md)
        assert "<h1>Heading</h1>" in html and "<strong>safe</strong>" in html
        assert "<script" not in html and "bad()" not in html
        events.append("owner-write")
        return fact

    monkeypatch.setattr(authoring, "render_markdown", renderer)
    monkeypatch.setattr(learning, "author_chapter", owner)
    for _ in range(2):
        assert (
            authoring.upsert_chapter(
                session,
                tenant_id=tenant_id,
                course_id=course_id,
                number=2,
                title="Chapter",
                body_md=raw,
                part=part,
            )
            is fact
        )
    assert events == ["owner-admitted", "render", "owner-write"] * 2


def test_default_part_stays_empty_and_missing_course_never_renders(monkeypatch):
    session = object()
    tenant_id, course_id = uuid4(), uuid4()
    failure = learning.CurriculumNotFound("course not found for tenant")

    def owner(db, command, *, render_markdown):
        assert db is session and command.part == ""
        assert render_markdown is authoring.render_markdown
        raise failure

    monkeypatch.setattr(
        authoring,
        "render_markdown",
        lambda body: pytest.fail("rendered before canonical course lookup"),
    )
    monkeypatch.setattr(learning, "author_chapter", owner)
    with pytest.raises(NotFoundError, match="course not found for tenant") as raised:
        authoring.upsert_chapter(
            session,
            tenant_id=tenant_id,
            course_id=course_id,
            number=2,
            title="Chapter",
            body_md="raw",
        )
    assert raised.value.__cause__ is None and raised.value.__suppress_context__ is True


def test_renderer_failure_propagates_before_owner_write(monkeypatch):
    session = object()
    tenant_id, course_id = uuid4(), uuid4()
    failure = RuntimeError("renderer failed")
    events = []

    def renderer(body):
        events.append("render")
        raise failure

    def owner(db, command, *, render_markdown):
        assert db is session and render_markdown is renderer
        events.append("owner-admitted")
        render_markdown(command.body_md)
        pytest.fail("write followed failed renderer")

    monkeypatch.setattr(authoring, "render_markdown", renderer)
    monkeypatch.setattr(learning, "author_chapter", owner)
    with pytest.raises(RuntimeError) as raised:
        authoring.upsert_chapter(
            session,
            tenant_id=tenant_id,
            course_id=course_id,
            number=2,
            title="Chapter",
            body_md="raw",
        )
    assert raised.value is failure and events == ["owner-admitted", "render"]


def test_delete_forwards_same_session_and_scope_without_local_work(monkeypatch):
    session = object()
    tenant_id, course_id, chapter_id = uuid4(), uuid4(), uuid4()
    calls = []

    def owner(db, **kw):
        assert db is session and kw == {
            "tenant_id": tenant_id,
            "course_id": course_id,
            "chapter_id": chapter_id,
        }
        calls.append(kw)

    monkeypatch.setattr(learning, "delete_chapter", owner)
    assert authoring.delete_chapter(session, tenant_id=tenant_id, course_id=course_id, chapter_id=chapter_id) is None
    assert len(calls) == 1


@pytest.mark.parametrize(
    "operation,detail",
    [
        ("create", "a course with slug 'mixed' already exists"),
        ("delete", "course not found for tenant"),
        ("delete", "chapter not found for tenant"),
    ],
)
def test_specific_domain_errors_preserve_product_messages_without_chaining(monkeypatch, operation, detail):
    session = object()
    tenant_id, course_id, chapter_id = uuid4(), uuid4(), uuid4()
    failure = learning.CurriculumConflict(detail) if operation == "create" else learning.CurriculumNotFound(detail)

    def owner(*args, **kw):
        raise failure

    if operation == "create":
        monkeypatch.setattr(learning, "create_draft_course", owner)
        with pytest.raises(ConflictError) as raised:
            authoring.create_course(
                session,
                tenant_id=tenant_id,
                slug="mixed",
                title="Title",
                discipline="network",
            )
    else:
        monkeypatch.setattr(learning, "delete_chapter", owner)
        with pytest.raises(NotFoundError) as raised:
            authoring.delete_chapter(session, tenant_id=tenant_id, course_id=course_id, chapter_id=chapter_id)
    assert str(raised.value) == detail and raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True


@pytest.mark.parametrize("operation", ["create", "chapter", "delete"])
def test_required_public_failures_are_not_mapped_or_suppressed(monkeypatch, operation):
    session = object()
    tenant_id, course_id, chapter_id = uuid4(), uuid4(), uuid4()
    failure = learning.TransactionChanged("caller root changed")

    def owner(*args, **kw):
        raise failure

    monkeypatch.setattr(
        learning,
        {
            "create": "create_draft_course",
            "chapter": "author_chapter",
            "delete": "delete_chapter",
        }[operation],
        owner,
    )
    with pytest.raises(learning.TransactionChanged) as raised:
        if operation == "create":
            authoring.create_course(
                session,
                tenant_id=tenant_id,
                slug="raw",
                title="Title",
                discipline="network",
            )
        elif operation == "chapter":
            authoring.upsert_chapter(
                session,
                tenant_id=tenant_id,
                course_id=course_id,
                number=2,
                title="Title",
                body_md="raw",
            )
        else:
            authoring.delete_chapter(session, tenant_id=tenant_id, course_id=course_id, chapter_id=chapter_id)
    assert raised.value is failure


def test_actual_renderer_sanitizes_before_safe_media_embedding():
    html = authoring.render_markdown(
        "**safe**\n\n<script>bad()</script>\n\n"
        '<img src="javascript:bad()" onerror="bad()">\n\n'
        "[clip](https://youtu.be/abc_123)\n\n[video](https://cdn.example.test/clip.mp4)"
    )
    assert "<strong>safe</strong>" in html
    assert "<script" not in html and "onerror" not in html and "javascript:" not in html
    assert 'src="#"' in html
    assert 'src="https://www.youtube.com/embed/abc_123"' in html
    assert '<video controls src="https://cdn.example.test/clip.mp4"></video>' in html


@pytest.mark.parametrize("source", ["  # Editable source  ", ""])
def test_editor_reads_only_public_fact_text_fields(source):
    tenant_id, course_id = uuid4(), uuid4()
    fact = _chapter(tenant_id, course_id, source, "<h2>Heading</h2><p>Hello &amp; world<br>Next</p>")
    text = authoring.editable_chapter_source(fact)
    if source:
        assert text == source
    else:
        assert "Heading" in text and "Hello & world" in text and "Next" in text
        assert "<h2>" not in text and "<p>" not in text


@pytest.fixture(autouse=True)
def _explicit_learning_policy_settings(monkeypatch):
    # Characterization data belongs to the product fixture, never the library.
    from app.services import learning_policy, settings_store

    original = settings_store.effective

    def effective(db, *, require_store=False):
        if require_store:
            return {
                "learning_missing_question_weight": 1,
                "learning_default_extra_attempts": 1,
                "learning_default_course_status": "published",
                "learning_default_course_listed": False,
                "learning_exam_seconds_per_question": 12.0,
                "learning_exam_near_chance_margin": 1.33,
                "learning_exam_fallback_score_floor": 1.0 / 3.0,
                "learning_exam_fallback_duration_seconds": 360.0,
                "learning_prerequisite_default_kind": "course_completed",
                "learning_prerequisite_default_composition": "all",
                "learning_track_default_gating_mode": "previous",
                "learning_default_completion_fraction": 1.0,
                "learning_default_certificate_auto_issue": True,
                "learning_default_certificate_validity_days": 0,
                "learning_default_activity_scoring_policy": "best",
                "learning_default_attempt_cooldown_seconds": 0,
                "learning_session_promotes_delivery_mode": True,
                "learning_pacing_default_open": True,
            }
        return original(db)

    monkeypatch.setattr(settings_store, "effective", effective)
    monkeypatch.setattr(learning_policy, "effective", effective)
