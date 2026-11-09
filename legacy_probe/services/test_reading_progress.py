"""Durable subtopic progress — the state that replaced browser localStorage.

Requires a migrated disposable Postgres (skipped otherwise by the fixtures).
"""

from __future__ import annotations

from app.models.course import Chapter, Course
from app.services import reading_progress
from legacy_probe.conftest import make_person_party


def _seed(db, tid, *, slug="rp-course"):
    course = Course(tenant_id=tid, slug=slug, title="RP", discipline="networking",
                    source_ref="x", version=1, status="published", listed=False)
    person = make_person_party(db, tenant_id=tid, email=f"{slug}@a.edu", first_name="R", last_name="P")
    db.add_all([course, person])
    db.flush()
    ch1 = Chapter(tenant_id=tid, course_id=course.id, number=1, title="One", part="I",
                  body_html="<h2>Alpha</h2><h2>Beta</h2>", source_hash="h1", order_index=1)
    ch2 = Chapter(tenant_id=tid, course_id=course.id, number=2, title="Two", part="I",
                  body_html="<h2>Gamma</h2>", source_hash="h2", order_index=2)
    db.add_all([ch1, ch2])
    db.flush()
    return course, person, ch1, ch2

def test_mark_complete_is_idempotent(admin_session, tenant_a):
    tid = tenant_a.id
    _, person, ch1, _ = _seed(admin_session, tid, slug="rp-idem")

    first = reading_progress.mark_complete(
        admin_session, tenant_id=tid, person_id=person.id, chapter_id=ch1.id, subtopic_slug="alpha"
    )
    second = reading_progress.mark_complete(
        admin_session, tenant_id=tid, person_id=person.id, chapter_id=ch1.id, subtopic_slug="alpha"
    )
    done = reading_progress.completed_slugs(
        admin_session, tenant_id=tid, person_id=person.id, chapter_id=ch1.id
    )
    admin_session.rollback()

    assert first is True   # created
    assert second is False  # already there, no duplicate row
    assert done == {"alpha"}

def test_progress_is_scoped_to_person_and_chapter(admin_session, tenant_a):
    tid = tenant_a.id
    _, person, ch1, ch2 = _seed(admin_session, tid, slug="rp-scope")
    other = make_person_party(admin_session, tenant_id=tid, email="rp-other@a.edu", first_name="O", last_name="T")
    admin_session.add(other)
    admin_session.flush()

    reading_progress.mark_complete(admin_session, tenant_id=tid, person_id=person.id,
                                   chapter_id=ch1.id, subtopic_slug="alpha")
    reading_progress.mark_complete(admin_session, tenant_id=tid, person_id=person.id,
                                   chapter_id=ch2.id, subtopic_slug="gamma")
    reading_progress.mark_complete(admin_session, tenant_id=tid, person_id=other.id,
                                   chapter_id=ch1.id, subtopic_slug="beta")

    mine_ch1 = reading_progress.completed_slugs(
        admin_session, tenant_id=tid, person_id=person.id, chapter_id=ch1.id
    )
    theirs_ch1 = reading_progress.completed_slugs(
        admin_session, tenant_id=tid, person_id=other.id, chapter_id=ch1.id
    )
    admin_session.rollback()

    assert mine_ch1 == {"alpha"}      # not polluted by ch2 or by the other learner
    assert theirs_ch1 == {"beta"}

def test_batched_lookup_returns_one_entry_per_chapter(admin_session, tenant_a):
    tid = tenant_a.id
    _, person, ch1, ch2 = _seed(admin_session, tid, slug="rp-batch")
    reading_progress.mark_complete(admin_session, tenant_id=tid, person_id=person.id,
                                   chapter_id=ch1.id, subtopic_slug="alpha")
    reading_progress.mark_complete(admin_session, tenant_id=tid, person_id=person.id,
                                   chapter_id=ch1.id, subtopic_slug="beta")
    reading_progress.mark_complete(admin_session, tenant_id=tid, person_id=person.id,
                                   chapter_id=ch2.id, subtopic_slug="gamma")

    by_chapter = reading_progress.completed_slugs_by_chapter(
        admin_session, tenant_id=tid, person_id=person.id, chapter_ids=[ch1.id, ch2.id]
    )
    empty = reading_progress.completed_slugs_by_chapter(
        admin_session, tenant_id=tid, person_id=person.id, chapter_ids=[]
    )
    admin_session.rollback()

    assert by_chapter == {ch1.id: {"alpha", "beta"}, ch2.id: {"gamma"}}
    assert empty == {}

def test_blank_slug_is_not_recorded(admin_session, tenant_a):
    tid = tenant_a.id
    _, person, ch1, _ = _seed(admin_session, tid, slug="rp-blank")

    created = reading_progress.mark_complete(
        admin_session, tenant_id=tid, person_id=person.id, chapter_id=ch1.id, subtopic_slug="   "
    )
    done = reading_progress.completed_slugs(
        admin_session, tenant_id=tid, person_id=person.id, chapter_id=ch1.id
    )
    admin_session.rollback()

    assert created is False
    assert done == set()
