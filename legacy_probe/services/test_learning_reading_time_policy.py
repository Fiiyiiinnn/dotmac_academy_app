"""Product reading-time policy and presentation boundary canaries."""

from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from app.config import Settings, settings, validate_learning_policy_value
from app.services import learning_policy, settings_store


@pytest.mark.parametrize(
    ("rate", "minimum", "words", "expected"),
    [(120, 0, 0, 0), (120, 3, 0, 3), (120, 0, 300, 2), (120, 0, 420, 4), (90, 2, 450, 5)],
)
def test_reading_time_composes_product_settings_and_preserves_half_even_rounding(
    monkeypatch, rate, minimum, words, expected
):
    db = object()
    read = Mock(return_value={})
    monkeypatch.setattr(settings, "learning_reading_words_per_minute", rate)
    monkeypatch.setattr(settings, "learning_reading_minimum_minutes", minimum)
    monkeypatch.setattr(settings_store, "get_all", read)
    assert learning_policy.reading_minutes(db, words) == expected
    read.assert_called_once_with(db)


@pytest.mark.parametrize("key", ["learning_reading_words_per_minute", "learning_reading_minimum_minutes"])
@pytest.mark.parametrize("value", [None, True, 1.5, "1.5", "-1", "nan"])
def test_malformed_reading_policy_is_rejected_by_settings_and_store_validation(key, value):
    with pytest.raises(ValueError):
        validate_learning_policy_value(key, value)
    with pytest.raises(ValueError):
        Settings(**{key: value})


def test_reading_rate_must_be_positive_and_minimum_may_be_zero():
    with pytest.raises(ValueError):
        validate_learning_policy_value("learning_reading_words_per_minute", "0")
    with pytest.raises(ValueError):
        Settings(learning_reading_words_per_minute=0)
    assert validate_learning_policy_value("learning_reading_minimum_minutes", "0") == 0


def test_reading_time_requires_session_and_available_store(monkeypatch):
    with pytest.raises(ValueError, match="session"):
        learning_policy.reading_minutes(None, 300)
    read = Mock(side_effect=RuntimeError("policy store unavailable"))
    monkeypatch.setattr(settings_store, "get_all", read)
    with pytest.raises(RuntimeError, match="policy store unavailable"):
        learning_policy.reading_minutes(object(), 300)


@pytest.mark.parametrize("key", ["learning_reading_words_per_minute", "learning_reading_minimum_minutes"])
def test_stored_null_reading_policy_never_falls_back(monkeypatch, key):
    monkeypatch.setattr(settings_store, "get_all", Mock(return_value={key: None}))
    with pytest.raises(ValueError):
        learning_policy.reading_minutes(object(), 300)


def test_database_reading_policy_overrides_settings_without_committing(admin_session, monkeypatch):
    admin_session.connection()  # begin the root explicitly (no autobegin yet)
    root = admin_session.get_transaction()
    assert root is not None
    monkeypatch.setattr(settings, "learning_reading_words_per_minute", 75)
    monkeypatch.setattr(settings, "learning_reading_minimum_minutes", 9)
    settings_store.set_many(
        admin_session,
        {"learning_reading_words_per_minute": "120", "learning_reading_minimum_minutes": "0"},
    )
    assert learning_policy.reading_minutes(admin_session, 300) == 2
    assert learning_policy.reading_minutes(admin_session, 0) == 0
    assert admin_session.get_transaction() is root
    assert root.is_active
    with pytest.raises(ValueError):
        settings_store.set_many(admin_session, {"learning_reading_words_per_minute": "0"})
    assert learning_policy.reading_minutes(admin_session, 300) == 2
    admin_session.rollback()


def test_missing_effective_reading_key_is_not_a_runtime_fallback(monkeypatch):
    monkeypatch.setattr(learning_policy, "effective", Mock(return_value={}))
    with pytest.raises(KeyError):
        learning_policy.reading_minutes(object(), 300)


@pytest.mark.parametrize("body", [None, "", "<p>one</p><p>two three</p>"])
@pytest.mark.parametrize("route", ["preview", "learner"])
def test_both_presentations_apply_same_composed_policy_after_legacy_html_word_count(monkeypatch, body, route):
    from app.web import instructor, learn

    tenant = SimpleNamespace(id=uuid4())
    person = SimpleNamespace(id=uuid4())
    course = SimpleNamespace(id=uuid4(), slug="reading-policy")
    chapter = SimpleNamespace(id=uuid4(), number=1, body_html=body)
    db = Mock()
    request = object()
    monkeypatch.setattr(settings, "learning_reading_words_per_minute", 2)
    monkeypatch.setattr(settings, "learning_reading_minimum_minutes", 0)
    observations = []

    def read_policy(session):
        assert session is db
        observations.append("policy")
        return {}

    monkeypatch.setattr(settings_store, "get_all", read_policy)
    render = Mock(side_effect=lambda _request, _template, context: context)
    monkeypatch.setattr(instructor.templates, "TemplateResponse", render)
    if route == "preview":

        def read_activity(_statement):
            observations.append("activity")
            return Mock(first=Mock(return_value=None))

        db.scalars.side_effect = read_activity
        result = instructor._preview_course_response(request, db, tenant.id, course, [chapter], chapter)
        assert observations == ["activity", "policy"]
    else:
        note = SimpleNamespace(body="Owner note")
        bookmark_state = Mock(return_value={"bookmarked": True, "note": note})
        monkeypatch.setattr(learn.bookmarks, "get_state", bookmark_state)
        db.scalars.side_effect = [
            Mock(first=Mock(return_value=course)),
            Mock(first=Mock(return_value=chapter)),
            Mock(all=Mock(return_value=[])),
            Mock(all=Mock(return_value=[chapter])),
            Mock(first=Mock(return_value=None)),
            Mock(first=Mock(return_value=None)),
        ]
        db.scalar.return_value = 1
        monkeypatch.setattr(learn, "require_tenant", Mock(return_value=tenant))
        monkeypatch.setattr(learn, "require_course_open", Mock())
        monkeypatch.setattr(learn.learning_events, "emit", Mock())
        monkeypatch.setattr(learn.reading_progress, "completed_slugs_by_chapter", Mock(return_value={}))

        def completed_ids(session, **kwargs):
            assert session is db
            observations.append("completion")
            return set()

        monkeypatch.setattr(learn.reading_progress, "completed_chapter_ids", completed_ids)
        monkeypatch.setattr(learn, "best_scores_for", Mock(return_value={}))
        result = learn.chapter(course.slug, 1, request, person=person, db=db)
        assert observations == ["completion", "policy", "completion"]
        assert db.scalars.call_count == 6
        bookmark_state.assert_called_once_with(
            db, tenant_id=tenant.id, person_id=person.id, chapter_id=chapter.id
        )
        assert result["bookmarked"] is True
        assert result["note"] is note
    assert result["reading_minutes"] == (2 if body else 0)
    db.add.assert_not_called()
    db.flush.assert_not_called()
    db.commit.assert_not_called()
    db.rollback.assert_not_called()


def test_preview_without_chapter_keeps_absence_sentinel_and_does_not_read_policy(monkeypatch):
    from app.web import instructor

    read = Mock(side_effect=AssertionError("absent chapter has no reading policy"))
    monkeypatch.setattr(settings_store, "get_all", read)
    monkeypatch.setattr(instructor.templates, "TemplateResponse", lambda _request, _template, context: context)
    db = Mock()
    result = instructor._preview_course_response(object(), db, uuid4(), SimpleNamespace(id=uuid4()), [], None)
    assert result["reading_minutes"] == 0
    db.scalars.assert_not_called()
    read.assert_not_called()
