"""Academy presentation adapters for public Learning insight projections."""

# Ported at new-composition genesis (learning-facing wave).

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import dotmac_learning as learning
from sqlalchemy.orm import Session

from app.services.learning_policy import cohort_insight_terms, learner_insight_terms

_RECOMMENDATIONS = {
    "overdue": "Submit this activity now — it is overdue.",
    "attempts_exhausted": "Ask your instructor about a reset or guidance.",
    "start_first_chapter": "Start your first chapter — everything begins there.",
    "continue_unfinished": "Keep going: your next unfinished activity is on the dashboard.",
}
_BLOCKER_RULES = {
    "overdue": "due date passed without a passing score",
    "attempts_exhausted": "maximum attempts used without a pass",
}


def learner_activity(
    db: Session,
    *,
    tenant_id: UUID,
    person_id: UUID,
    now: datetime | None = None,
) -> dict:
    """The 'My learning activity' projection for the progress page."""
    terms = learner_insight_terms(db)
    fact = learning.learner_activity(db, tenant_id=tenant_id, person_id=person_id, terms=terms, now=now)
    blocker = None
    if fact.blocker is not None:
        value = fact.blocker
        blocker = {
            "activity_id": value.activity_id,
            "title": value.title,
            "course_id": value.course_id,
            "reason": value.reason,
            "rule": _BLOCKER_RULES[value.reason],
            "due_at": value.due_at,
            "action": _RECOMMENDATIONS[value.reason],
        }
    return {
        "weekly": [{"week_start": b.week_start, "count": b.count} for b in fact.weekly],
        "max_week": fact.max_week,
        "velocity_per_week": fact.velocity_per_week,
        "grade_trend": {
            "points": list(fact.grade_trend.points),
            "direction": fact.grade_trend.direction,
        },
        "days_since_last": fact.days_since_last,
        "disciplines": {
            "strong": fact.disciplines.strong,
            "weak": fact.disciplines.weak,
            "all": {s.discipline: s.pct for s in fact.disciplines.all},
        },
        "attendance": fact.attendance,
        "blocker": blocker,
        "recommendation": (
            (
                "Pick up where you left off — "
                f"{'a week' if terms.resume_after_days == 7 else str(terms.resume_after_days) + ' days'} "
                "away is easy to recover."
            )
            if fact.recommendation == "resume_after_week"
            else _RECOMMENDATIONS[fact.recommendation]
        ),
    }


def _cohort_course_ids(db: Session, tenant_id: UUID, cohort_id: UUID) -> list[UUID]:
    """Preserve the CSV consumer's helper while Learning owns its query."""
    return learning.cohort_course_ids(db, tenant_id=tenant_id, cohort_id=cohort_id)


def cohort_overview(
    db: Session,
    *,
    tenant_id: UUID,
    cohort_id: UUID,
    now: datetime | None = None,
) -> dict:
    terms = cohort_insight_terms(db)
    fact = learning.insight_cohort_overview(db, tenant_id=tenant_id, cohort_id=cohort_id, terms=terms, now=now)
    return {
        "learners": fact.learners,
        "active": fact.active,
        "inactive": fact.inactive,
        "active_rule": f"any learning event in the last {fact.active_window_days} days",
        "submitted": fact.submitted,
        "overdue": fact.overdue,
        "not_attempted": fact.not_attempted,
        "avg_score_pct": fact.avg_score_pct,
        "funnel": {
            "enrolled": fact.funnel.enrolled,
            "started": fact.funnel.started,
            "half": fact.funnel.half,
            "completed": fact.funnel.completed,
            "certified": fact.funnel.certified,
        },
        "lab_failures": [
            {
                "course": f.course_title if f.course_title is not None else "—",
                "failed_checks": f.failed_checks,
            }
            for f in fact.lab_failures
        ],
        "week_events": fact.week_events,
        "prior_week_avg": fact.prior_week_avg,
    }


def cohorts_comparison(db: Session, *, tenant_id: UUID, now: datetime | None = None) -> list[dict]:
    """One comparison row per cohort for the reports index."""
    return [
        {
            "cohort": row.cohort,
            "learners": row.learners,
            "active": row.active,
            "completed": row.completed,
            "completion_pct": row.completion_pct,
        }
        for row in learning.cohorts_comparison(
            db, tenant_id=tenant_id, active_window_days=cohort_insight_terms(db).active_window_days, now=now
        )
    ]
