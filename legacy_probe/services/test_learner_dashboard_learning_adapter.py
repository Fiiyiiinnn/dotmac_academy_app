"""Hosted boundary tests using public Learning facts and Academy presentation."""

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import Mock
from uuid import uuid4

import dotmac_learning as learning
import pytest

from app.services import learner_dashboard as adapter

NOW = datetime(2026, 10, 17, 12, tzinfo=UTC)


@pytest.fixture
def facts():
    tenant, person, course_id = uuid4(), uuid4(), uuid4()
    course = learning.CourseFact(
        id=course_id,
        tenant_id=tenant,
        slug="source-course",
        title="Source course",
        discipline="x",
        source_ref="in-app",
        version=1,
        status="published",
        listed=True,
        created_at=NOW,
        updated_at=NOW,
    )
    chapter = learning.ChapterFact(
        id=uuid4(),
        tenant_id=tenant,
        course_id=course_id,
        number=5,
        title="Resume here",
        part="",
        body_md="",
        body_html="",
        source_hash="",
        order_index=5,
    )
    activity = learning.ActivityFact(
        id=uuid4(),
        tenant_id=tenant,
        course_id=course_id,
        title="Next activity",
        chapter_number=5,
        type="lab",
        bank_id=None,
        pass_threshold=0.73,
        max_attempts=3,
        assessment_mode="graded",
        grading="auto",
        weight=2,
        question_count=None,
        created_at=NOW,
        updated_at=NOW,
    )
    score = learning.ScoreFact(
        id=uuid4(),
        tenant_id=tenant,
        submission_id=uuid4(),
        score=3,
        max_score=7,
        fraction=3 / 7,
        passed=False,
        per_item=None,
        source="auto",
        override_reason=None,
        created_at=NOW,
        updated_at=NOW,
    )
    session = learning.ClassSessionFact(
        id=uuid4(),
        tenant_id=tenant,
        cohort_id=uuid4(),
        offering_id=None,
        session_type="class",
        title="Next session",
        starts_at=NOW,
        ends_at=None,
        instructor_person_id=None,
        location="Room",
        join_url=None,
        notes=None,
        status="scheduled",
        created_at=NOW,
        updated_at=NOW,
    )
    return tenant, person, course, chapter, activity, score, session


def _card(facts, action):
    _, _, course, _, activity, _, session = facts
    return learning.CourseCard(
        course=course,
        state="in_progress",
        pct=27,
        passed=2,
        total=7,
        grade_pct=43,
        final_threshold_pct=73,
        next_activity=activity,
        next_deadline=NOW,
        starts_at=NOW,
        last_read=learning.LastRead(4, "Last read"),
        next_session=session,
        certificate_status=None,
        locked_reason=None,
        action=action,
    )


@pytest.mark.parametrize(
    "code, chapter_number, label, suffix",
    [
        ("start", 1, "Start", "/courses/source-course/chapters/1"),
        ("resume", 5, "Resume", "/courses/source-course/chapters/5"),
        ("review_feedback", None, "Review feedback", None),
        (None, None, None, None),
    ],
)
def test_card_semantic_actions_and_attribute_leaves_match_legacy_consumers(
    monkeypatch, facts, code, chapter_number, label, suffix
):
    tenant, person, course, _, activity, _, session = facts
    action = learning.DashboardAction(code, course.id, chapter_number) if code else None
    projection = learning.CourseCards(
        (_card(facts, action),),
        (learning.StateCount("in_progress", 1), learning.StateCount("locked", 0)),
        ("in_progress", "locked"),
    )
    facade = Mock(return_value=projection)
    monkeypatch.setattr(learning, "course_cards", facade)
    db = object()
    result = adapter.course_cards(db, tenant_id=tenant, person_id=person, now=NOW)
    facade.assert_called_once_with(db, tenant_id=tenant, person_id=person, now=NOW)
    assert result["counts"] == {"in_progress": 1, "locked": 0}
    assert result["filters"] == ["in_progress", "locked"]
    (card,) = result["cards"]
    assert set(card) == {
        "course",
        "state",
        "pct",
        "passed",
        "total",
        "grade_pct",
        "final_threshold_pct",
        "next_activity",
        "next_deadline",
        "starts_at",
        "last_read",
        "next_session",
        "certificate_status",
        "locked_reason",
        "action",
    }
    # Home and digest consume attributes, not recursively converted dictionaries.
    assert card["course"] is course and card["course"].slug == "source-course"
    assert card["next_activity"] is activity and card["next_activity"].title == "Next activity"
    assert card["next_session"] is session and card["next_session"].location == "Room"
    assert (card["pct"], card["passed"], card["total"], card["grade_pct"]) == (
        27,
        2,
        7,
        43,
    )
    assert card["next_deadline"] == card["starts_at"] == NOW
    assert card["last_read"] == {"number": 4, "title": "Last read"}
    if code:
        assert card["action"] == {
            "label": label,
            "href": suffix or f"/progress#course-{course.id}",
        }
    else:
        assert card["action"] is None


def test_home_uses_one_public_batched_projection_and_keeps_continue_attributes(monkeypatch, facts):
    tenant, person, course, chapter, *_ = facts
    projection = learning.LearnerHome(
        (),
        (learning.StateCount("locked", 0),),
        ("locked",),
        learning.ContinueTarget(course, chapter),
    )
    facade = Mock(return_value=projection)
    monkeypatch.setattr(learning, "learner_home", facade)
    monkeypatch.setattr(learning, "course_cards", Mock(side_effect=AssertionError("extra fanout")))
    monkeypatch.setattr(learning, "continue_target", Mock(side_effect=AssertionError("extra fanout")))
    db = object()
    result = adapter.learner_home(db, tenant_id=tenant, person_id=person, now=NOW)
    facade.assert_called_once_with(db, tenant_id=tenant, person_id=person, now=NOW)
    assert result == {
        "cards": [],
        "counts": {"locked": 0},
        "filters": ["locked"],
        "continue_to": {"course": course, "chapter": chapter},
    }
    assert result["continue_to"]["chapter"] is chapter


def test_single_course_and_cross_course_resume_delegate_without_legacy_context(monkeypatch, facts):
    tenant, person, course, chapter, *_ = facts
    resume = Mock(return_value=chapter)
    target = Mock(return_value=learning.ContinueTarget(course, chapter))
    monkeypatch.setattr(learning, "resume_chapter", resume)
    monkeypatch.setattr(learning, "continue_target", target)
    db = object()
    assert adapter.resume_chapter(db, tenant_id=tenant, person_id=person, course_id=course.id) is chapter
    resume.assert_called_once_with(db, tenant_id=tenant, person_id=person, course_id=course.id)
    assert adapter.continue_target(db, tenant_id=tenant, person_id=person, _ctx={"retired": object()}) == {
        "course": course,
        "chapter": chapter,
    }
    target.assert_called_once_with(db, tenant_id=tenant, person_id=person)
    target.return_value = None
    assert adapter.continue_target(db, tenant_id=tenant, person_id=person) is None


@pytest.mark.parametrize(
    "certificate",
    [
        learning.ProgressCertificate("issued", "CERT-OLD", None),
        learning.ProgressCertificate("eligible", None, uuid4()),
        learning.ProgressCertificate(None, None, None),
    ],
)
def test_progress_preserves_module_decisions_order_shared_rows_and_certificate_target(monkeypatch, facts, certificate):
    tenant, person, course, _, activity, score, _ = facts
    upcoming = learning.ProgressActivity(activity, 5, score, 43, False, 73, 2, 5, 3, 2, True, NOW, False)
    overdue = learning.ProgressActivity(
        replace(activity, id=uuid4(), title="Late activity"),
        None,
        None,
        None,
        False,
        73,
        0,
        None,
        None,
        0,
        False,
        NOW,
        True,
    )
    projection = learning.CourseProgress(
        course,
        43,
        27,
        73,
        (upcoming, overdue),
        (upcoming,),
        (overdue,),
        (81, 23, 46),
        "down",
        certificate,
    )
    facade = Mock(return_value=(projection,))
    progress_trend = object()
    feedback_terms = object()
    monkeypatch.setattr(adapter, "progress_trend_terms", lambda session: progress_trend)
    monkeypatch.setattr(adapter, "feedback_reveal_terms", lambda session: feedback_terms)
    monkeypatch.setattr(learning, "progress_overview", facade)
    db = object()
    overview = adapter.progress_overview(db, tenant_id=tenant, person_id=person, now=NOW)
    facade.assert_called_once_with(
        db, tenant_id=tenant, person_id=person, progress_trend=progress_trend, feedback_terms=feedback_terms, now=NOW
    )
    (item,) = overview
    assert item["course"] is course
    assert (item["grade_pct"], item["completion_pct"], item["final_threshold_pct"]) == (
        43,
        27,
        73,
    )
    assert item["trend_points"] == [81, 23, 46] and item["trend"] == "down"
    row, late = item["rows"]
    assert item["upcoming"][0] is row and item["overdue"][0] is late
    assert row["activity"] is activity and row["score"] is score
    assert row["score"].score == 3 and row["score"].per_item is None
    assert (
        row["attempts_used"],
        row["max_attempts"],
        row["attempts_left"],
        row["attempts_granted"],
    ) == (2, 5, 3, 2)
    assert row["feedback_available"] and row["due_at"] == NOW
    assert late["overdue"] and late["score"] is None and late["attempts_left"] is None
    assert item["certificate"] == {
        "status": certificate.status,
        "serial": certificate.serial,
        "href": f"/certificates/{certificate.target_course_id}" if certificate.target_course_id is not None else None,
    }


def test_facade_read_failures_propagate(monkeypatch, facts):
    tenant, person, *_ = facts
    monkeypatch.setattr(adapter, "progress_trend_terms", lambda session: object())
    monkeypatch.setattr(adapter, "feedback_reveal_terms", lambda session: object())
    monkeypatch.setattr(learning, "progress_overview", Mock(side_effect=RuntimeError("read failed")))
    with pytest.raises(RuntimeError, match="read failed"):
        adapter.progress_overview(object(), tenant_id=tenant, person_id=person)
