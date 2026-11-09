"""Academy adapters for the public learning-event commands and readers.

The caller owns the Session and transaction. Required event writes propagate
failures. Optional emission delegates SAVEPOINT isolation to learning; pending
host work is required and its flush failures propagate to the caller.
"""

# Ported at new-composition genesis (learning-facing wave).

from __future__ import annotations

import logging
from datetime import datetime
from uuid import UUID

import dotmac_learning as learning
from sqlalchemy.orm import Session

from app.services.learning_policy import learner_insight_terms
from app.services.localtime import academy_zone

logger = logging.getLogger(__name__)

EVENT_KINDS = learning.EVENT_KINDS
KIND_CERTIFICATE_EARNED = learning.KIND_CERTIFICATE_EARNED
KIND_CHAPTER_COMPLETED = learning.KIND_CHAPTER_COMPLETED
KIND_CHAPTER_VIEWED = learning.KIND_CHAPTER_VIEWED
KIND_COURSE_VIEWED = learning.KIND_COURSE_VIEWED


def record(
    db: Session,
    *,
    tenant_id: UUID,
    person_id: UUID,
    kind: str,
    course_id: UUID | None = None,
    subject_id: UUID | None = None,
    detail: dict | None = None,
    occurred_at: datetime | None = None,
) -> learning.EventFact | None:
    """Append one event; returns None when a dedupe rule suppressed it."""
    payload = learning.EventWrite(
        tenant_id=tenant_id,
        person_id=person_id,
        kind=kind,
        course_id=course_id,
        subject_id=subject_id,
        detail=detail or {},
        occurred_at=occurred_at,
    )
    return learning.record(
        db,
        payload,
        local_day_zone=(academy_zone() if kind in (KIND_COURSE_VIEWED, KIND_CHAPTER_VIEWED) else None),
    )


def emit(db: Session, **kwargs) -> None:
    """Best-effort event append; required pending host failures propagate."""
    # Keep required host work outside every optional construction/error handler.
    db.flush()
    try:
        payload = learning.EventWrite(
            **{
                **kwargs,
                "detail": kwargs.get("detail") or {},
            }
        )
        zone = academy_zone() if payload.kind in (KIND_COURSE_VIEWED, KIND_CHAPTER_VIEWED) else None
    except Exception as exc:
        logger.warning("learning-event emission failed (%s): %s", kwargs.get("kind"), exc)
        return None
    # The public facade owns its required preflush and optional SAVEPOINT.
    # Calling it outside the catch preserves its required failure boundary.
    outcome = learning.emit(db, payload, local_day_zone=zone)
    if outcome.status == "failed":
        logger.warning("learning-event emission failed (%s)", kwargs.get("kind"))
    return None


def last_activity_at(db: Session, *, tenant_id: UUID, person_id: UUID) -> datetime | None:
    return learning.last_activity_at(db, tenant_id=tenant_id, person_id=person_id)


def weekly_buckets(
    db: Session,
    *,
    tenant_id: UUID,
    person_id: UUID,
    weeks: int | None = None,
    kinds: tuple[str, ...] | None = None,
    now: datetime | None = None,
) -> list[dict]:
    """Per-ISO-week event counts for the trailing ``weeks`` weeks (oldest first)."""
    if weeks is None:
        weeks = learner_insight_terms(db).chart_weeks
    return [
        {"week_start": bucket.week_start, "count": bucket.count}
        for bucket in learning.weekly_buckets(
            db,
            tenant_id=tenant_id,
            person_id=person_id,
            weeks=weeks,
            kinds=kinds,
            now=now,
        )
    ]


def active_person_ids(
    db: Session,
    *,
    tenant_id: UUID,
    person_ids: list[UUID],
    since: datetime,
) -> set[UUID]:
    """Subset of ``person_ids`` with at least one ledger event since ``since``."""
    return learning.active_person_ids(db, tenant_id=tenant_id, person_ids=person_ids, since=since)


def kind_counts(
    db: Session,
    *,
    tenant_id: UUID,
    kinds: tuple[str, ...],
    course_ids: list[UUID] | None = None,
    person_ids: list[UUID] | None = None,
    since: datetime | None = None,
) -> dict[UUID | None, dict[str, int]]:
    """Counts per (course_id, kind), optionally scoped to a cohort roster."""
    return learning.kind_counts(
        db,
        tenant_id=tenant_id,
        kinds=kinds,
        course_ids=course_ids,
        person_ids=person_ids,
        since=since,
    )
