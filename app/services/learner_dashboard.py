"""Academy presentation adapters for public learner dashboard projections."""

# Ported at new-composition genesis (learning-facing wave).

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import dotmac_learning as learning
from sqlalchemy.orm import Session

from app.services.learning_policy import feedback_reveal_terms, progress_trend_terms


def _continue(target: learning.ContinueTarget | None) -> dict | None:
    if target is None:
        return None
    return {"course": target.course, "chapter": target.chapter}


def _card(card: learning.CourseCard) -> dict:
    action = None
    if card.action is not None:
        code = card.action.code
        action = {
            "label": {
                "start": "Start",
                "resume": "Resume",
                "review_feedback": "Review feedback",
            }[code],
            "href": (
                f"/progress#course-{card.action.course_id}"
                if code == "review_feedback"
                else f"/courses/{card.course.slug}/chapters/{card.action.chapter_number}"
            ),
        }
    return {
        "course": card.course,
        "state": card.state,
        "pct": card.pct,
        "passed": card.passed,
        "total": card.total,
        "grade_pct": card.grade_pct,
        "final_threshold_pct": card.final_threshold_pct,
        "next_activity": card.next_activity,
        "next_deadline": card.next_deadline,
        "starts_at": card.starts_at,
        "last_read": (
            {"number": card.last_read.number, "title": card.last_read.title} if card.last_read is not None else None
        ),
        "next_session": card.next_session,
        "certificate_status": card.certificate_status,
        "locked_reason": card.locked_reason,
        "action": action,
    }


def _cards(projection: learning.CourseCards | learning.LearnerHome) -> dict:
    return {
        "cards": [_card(card) for card in projection.cards],
        "counts": {item.state: item.count for item in projection.counts},
        "filters": list(projection.filters),
    }


def resume_chapter(db: Session, *, tenant_id: UUID, person_id: UUID, course_id: UUID) -> learning.ChapterFact | None:
    """Return the module's resume chapter with its existing attribute interface."""
    return learning.resume_chapter(db, tenant_id=tenant_id, person_id=person_id, course_id=course_id)


def continue_target(db: Session, *, tenant_id: UUID, person_id: UUID, _ctx: dict | None = None) -> dict | None:
    """Adapt the public cross-course target.

    The private legacy ``_ctx`` optimization is retained in the signature only;
    the public home projection owns shared batching without product ORM context.
    """
    return _continue(learning.continue_target(db, tenant_id=tenant_id, person_id=person_id))


def learner_home(db: Session, *, tenant_id: UUID, person_id: UUID, now: datetime | None = None) -> dict:
    """Adapt cards and continue target from one shared public home projection."""
    projection = learning.learner_home(db, tenant_id=tenant_id, person_id=person_id, now=now)
    result = _cards(projection)
    result["continue_to"] = _continue(projection.continue_to)
    return result


def course_cards(
    db: Session,
    *,
    tenant_id: UUID,
    person_id: UUID,
    now: datetime | None = None,
    _ctx: dict | None = None,
) -> dict:
    """Adapt public card decisions to Academy labels, links and legacy containers.

    The private legacy ``_ctx`` argument is retained but no longer consumed.
    """
    return _cards(learning.course_cards(db, tenant_id=tenant_id, person_id=person_id, now=now))


def _progress_row(row: learning.ProgressActivity) -> dict:
    return {
        "activity": row.activity,
        "chapter_number": row.chapter_number,
        "score": row.score,
        "pct": row.pct,
        "passed": row.passed,
        "threshold_pct": row.threshold_pct,
        "attempts_used": row.attempts_used,
        "max_attempts": row.max_attempts,
        "attempts_left": row.attempts_left,
        "attempts_granted": row.attempts_granted,
        "feedback_available": row.feedback_available,
        "due_at": row.due_at,
        "overdue": row.overdue,
    }


def progress_overview(db: Session, *, tenant_id: UUID, person_id: UUID, now: datetime | None = None) -> list[dict]:
    """Adapt module progress without re-deriving grades, attempts or deadlines."""
    projections = learning.progress_overview(
        db,
        tenant_id=tenant_id,
        person_id=person_id,
        progress_trend=progress_trend_terms(db),
        feedback_terms=feedback_reveal_terms(db),
        now=now,
    )
    out = []
    for projection in projections:
        rows = {row.activity.id: _progress_row(row) for row in projection.rows}
        certificate = projection.certificate
        out.append(
            {
                "course": projection.course,
                "grade_pct": projection.grade_pct,
                "completion_pct": projection.completion_pct,
                "final_threshold_pct": projection.final_threshold_pct,
                "rows": [rows[row.activity.id] for row in projection.rows],
                "upcoming": [rows[row.activity.id] for row in projection.upcoming],
                "overdue": [rows[row.activity.id] for row in projection.overdue],
                "trend_points": list(projection.trend_points),
                "trend": projection.trend,
                "certificate": {
                    "status": certificate.status,
                    "serial": certificate.serial,
                    "href": f"/certificates/{certificate.target_course_id}"
                    if certificate.target_course_id is not None
                    else None,
                },
            }
        )
    return out
