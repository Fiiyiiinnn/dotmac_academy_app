"""In-app authoring service (Slice 5c, finding #8)."""

from __future__ import annotations

import pytest
from dotmac_learning import authoring_chapters, get_course

from app.services.authoring import create_course, render_markdown, upsert_chapter
from app.services.exceptions import ConflictError, NotFoundError


def test_create_course_is_draft_and_unique(admin_session, tenant_a):
    tid = tenant_a.id
    c = create_course(
        admin_session,
        tenant_id=tid,
        slug="New-Course",
        title="New",
        discipline="networking",
    )
    assert c.status == "draft"
    assert c.slug == "new-course"  # normalized
    with pytest.raises(ConflictError):
        create_course(
            admin_session,
            tenant_id=tid,
            slug="new-course",
            title="Dup",
            discipline="networking",
        )
    admin_session.rollback()


def test_upsert_chapter_renders_md_and_bumps_version(admin_session, tenant_a):
    tid = tenant_a.id
    c = create_course(admin_session, tenant_id=tid, slug="auth", title="Auth", discipline="networking")
    assert c.version == 1

    ch = upsert_chapter(
        admin_session,
        tenant_id=tid,
        course_id=c.id,
        number=1,
        title="Intro",
        body_md="# Hello\n\n- a\n- b",
    )
    assert "<h1>Hello</h1>" in ch.body_html
    assert ch.body_md.startswith("# Hello")
    current = get_course(admin_session, tenant_id=tid, course_id=c.id)
    assert current is not None
    assert current.version == 2  # bumped

    # Updating the same chapter number edits in place (no duplicate) and re-renders.
    ch2 = upsert_chapter(
        admin_session,
        tenant_id=tid,
        course_id=c.id,
        number=1,
        title="Intro v2",
        body_md="**bold**",
    )
    assert ch2.id == ch.id
    assert ch2.title == "Intro v2"
    assert "<strong>bold</strong>" in ch2.body_html
    current = get_course(admin_session, tenant_id=tid, course_id=c.id)
    assert current is not None
    assert current.version == 3
    n = len(authoring_chapters(admin_session, tenant_id=tid, course_id=c.id))
    assert n == 1
    admin_session.rollback()


def test_upsert_chapter_unknown_course_raises(admin_session, tenant_a):
    from uuid import uuid4

    with pytest.raises(NotFoundError):
        upsert_chapter(
            admin_session,
            tenant_id=tenant_a.id,
            course_id=uuid4(),
            number=1,
            title="X",
            body_md="x",
        )
    admin_session.rollback()


def test_render_markdown_tables():
    html = render_markdown("| a | b |\n|---|---|\n| 1 | 2 |")
    assert "<table>" in html


@pytest.fixture(autouse=True)
def _explicit_learning_policy_settings(monkeypatch):
    # Characterization data belongs to the product fixture, never the library.
    from app.services import settings_store

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
            }
        return original(db)

    monkeypatch.setattr(settings_store, "effective", effective)
