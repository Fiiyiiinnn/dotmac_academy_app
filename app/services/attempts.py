"""Academy signatures for Learning's canonical persisted attempt commands."""

# Ported at new-composition genesis (learning-facing wave).

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import dotmac_learning as learning
from dotmac_learning.attempt_models import ActivityAttempt
from sqlalchemy import select
from sqlalchemy.orm import Session


def open_or_create_attempt(
    db: Session,
    *,
    tenant_id: UUID,
    person_id: UUID,
    activity_id: UUID,
    all_ext_ids: list[str],
    count: int,
    now: datetime | None = None,
    questions: list | None = None,
    bank_version_id: UUID | None = None,
) -> learning.AttemptFact:
    """Return the public fact for the existing or newly drawn open attempt.

    ``bank_version_id`` pins the new attempt to the version the learner
    is being served; omitted, Learning pins the activity's version.
    """
    command = learning.OpenAttemptCommand(
        tenant_id=tenant_id,
        person_id=person_id,
        activity_id=activity_id,
        all_ext_ids=all_ext_ids,
        count=count,
        now=now,
        bank_version_id=bank_version_id,
        questions=None
        if questions is None
        else tuple(
            learning.SelectionQuestion(
                ext_id=q.ext_id,
                category=getattr(q, "category", None),
                options=getattr(q, "options", None),
            )
            for q in questions
        ),
    )
    return learning.open_or_create_attempt(db, command).attempt


def get_open_attempt(
    db: Session,
    *,
    tenant_id: UUID,
    person_id: UUID,
    activity_id: UUID,
) -> ActivityAttempt | None:
    """Read the open attempt without closing it (Learning has no read).

    Submit must observe the attempt still open: Learning grades against
    the open attempt's bank-version pin, so closing first would defeat
    the pin. Callers close after a successful submit instead.
    """
    return db.scalars(
        select(ActivityAttempt)
        .where(ActivityAttempt.tenant_id == tenant_id)
        .where(ActivityAttempt.person_id == person_id)
        .where(ActivityAttempt.activity_id == activity_id)
        .where(ActivityAttempt.submitted_at.is_(None))
        .order_by(ActivityAttempt.started_at.desc())
    ).first()


def close_open_attempt(
    db: Session,
    *,
    tenant_id: UUID,
    person_id: UUID,
    activity_id: UUID,
    now: datetime | None = None,
) -> learning.AttemptFact | None:
    """Return the closed attempt fact, or None when no attempt is open."""
    return learning.close_open_attempt(
        db,
        learning.CloseAttemptCommand(tenant_id=tenant_id, person_id=person_id, activity_id=activity_id, now=now),
    )
