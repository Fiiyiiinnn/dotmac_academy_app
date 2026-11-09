"""Academy timeline containers over detached Learning facts.

The nested score leaf is a RecentGradeFact, exposing the fraction/passed fields
used by Academy rendering. It is not a mutable Score ORM alias.
"""

# Ported at new-composition genesis (learning-facing wave).

from __future__ import annotations

from datetime import datetime
from typing import cast
from uuid import UUID

import dotmac_learning as learning
from sqlalchemy.orm import Session

from app.services.learning_policy import feedback_reveal_terms, todo_terms
from app.services.localtime import academy_zone
from app.services.settings_store import effective


def _deadline_row(row: learning.DeadlineFact) -> dict:
    return {
        "due_at": row.due_at,
        "activity": row.activity,
        "course_title": row.course_title,
        "course_slug": row.course_slug,
    }


def _deadline_rows(db: Session, *, tenant_id: UUID, person_id: UUID) -> list[dict]:
    """Retain the reminder caller's containers; Learning owns deadline selection."""
    return [_deadline_row(row) for row in learning.deadline_rows(db, tenant_id=tenant_id, person_id=person_id)]


def timeline_for_person(db: Session, *, tenant_id: UUID, person_id: UUID, now: datetime | None = None) -> dict:
    """Preserve product containers; historical bucket names do not set policy."""
    terms = todo_terms(db)
    policy = effective(db, require_store=True)
    timeline = learning.timeline_for_person(
        db,
        tenant_id=tenant_id,
        person_id=person_id,
        now=now,
        local_zone=academy_zone(),
        terms=terms,
        feedback_terms=feedback_reveal_terms(db),
        join_lead_minutes=cast(int, policy["learning_join_lead_minutes"]),
    )
    graded: list[dict] = [
        {
            "score": row,
            "activity": row.activity,
            "course_title": row.course_title,
            "course_slug": row.course_slug,
            "feedback_available": row.feedback_available,
        }
        for row in timeline.graded
    ]
    graded_by_id = {row["score"].score_id: row for row in graded}
    return {
        "near_deadline_days": terms.near_deadline_days,
        "far_deadline_days": terms.far_deadline_days,
        "window_horizon_days": terms.window_horizon_days,
        "overdue": [_deadline_row(row) for row in timeline.overdue],
        "due_today": [_deadline_row(row) for row in timeline.due_today],
        "next_7": [_deadline_row(row) for row in timeline.next_7],
        "next_30": [_deadline_row(row) for row in timeline.next_30],
        "sessions": [(entry.session, entry.cohort_name) for entry in timeline.sessions],
        "joinable": set(timeline.joinable),
        "graded": graded,
        "feedback": [graded_by_id[row.score_id] for row in timeline.feedback],
        "course_windows": [
            {"kind": row.kind, "at": row.at, "course_title": row.course_title} for row in timeline.course_windows
        ],
    }
