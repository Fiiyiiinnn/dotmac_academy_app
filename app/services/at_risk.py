"""At-risk / behind-pace detection + learner nudges.

Complements the agenda (which shows what's *upcoming*) by flagging what a
learner is *behind on*: a course whose offering window has closed without
completion (**overdue**), or where progress lags the elapsed window
(**behind**). Surfaced as in-app notifications via a daily sweep, mirroring
the email-digest job.

Signal uses only ``CourseCompletion.pct``/``status`` + the offering window, so
it needs no per-activity plumbing.
"""

# Ported at new-composition genesis (learning-facing wave 2).

from __future__ import annotations

from datetime import datetime
from typing import cast
from uuid import UUID

import dotmac_learning as learning
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.notification import Notification
from app.services import notifications
from app.services.settings_store import effective


def at_risk_for_person(
    db: Session,
    *,
    tenant_id: UUID,
    person_id: UUID,
    behind_threshold: float | None = None,
    now: datetime | None = None,
) -> list[dict]:
    """Keep nudge presentation fields; Learning owns tenant/window risk decisions."""
    if behind_threshold is None:
        behind_threshold = cast(float, effective(db, require_store=True)["learning_behind_pace_fraction"])
    rows = learning.at_risk_for_person(
        db, tenant_id=tenant_id, person_id=person_id, behind_threshold=behind_threshold, now=now
    )
    return [
        {
            "course_id": row.course_id,
            "title": row.title if row.title is not None else "your course",
            "reason": row.reason,
            "pct": row.pct,
            "expected": row.expected,
        }
        for row in rows
    ]


def notify_person_if_at_risk(db: Session, *, tenant_id: UUID, person_id: UUID, now: datetime | None = None) -> int:
    """Send one in-app nudge per at-risk course. Returns how many were sent.

    Deduped: skips a course that already has an unread at-risk nudge, so a
    daily sweep doesn't spam.
    """
    existing = set(
        db.scalars(
            select(Notification.link)
            .where(Notification.tenant_id == tenant_id)
            .where(Notification.person_id == person_id)
            .where(Notification.kind == "at_risk")
            .where(Notification.read_at.is_(None))
        ).all()
    )
    sent = 0
    for item in at_risk_for_person(db, tenant_id=tenant_id, person_id=person_id, now=now):
        link = f"/courses/{item['course_id']}"
        if link in existing:
            continue
        if item["reason"] == "overdue":
            title = f"Overdue: {item['title']}"
            body = "This course's window has closed and you haven't completed it yet."
        else:
            title = f"You're behind on {item['title']}"
            body = (
                f"You're at {round(item['pct'] * 100)}% but should be near "
                f"{round(item['expected'] * 100)}% by now. Catch up when you can."
            )
        notifications.notify(
            db,
            tenant_id=tenant_id,
            person_id=person_id,
            kind="at_risk",
            title=title,
            body=body,
            link=link,
        )
        sent += 1
    return sent
