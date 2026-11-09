"""Academy pacing dictionaries and HTTP errors from public Learning decisions."""

# Ported at new-composition genesis (learning-facing wave).

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import dotmac_learning as learning
from fastapi import HTTPException, status
from sqlalchemy.orm import Session


def activity_pacing(
    db: Session,
    *,
    tenant_id: UUID,
    person_id: UUID,
    course_id: UUID,
    activity_id: UUID,
    now: datetime | None = None,
) -> dict:
    """Return the legacy presentation dictionary without recalculating policy."""
    fact = learning.activity_pacing(
        db,
        tenant_id=tenant_id,
        person_id=person_id,
        course_id=course_id,
        activity_id=activity_id,
        now=now,
    )
    return {
        "readable": fact.readable,
        "submittable": fact.submittable,
        "has_override": fact.has_override,
    }


def require_activity_readable(
    db: Session,
    *,
    tenant_id,
    person_id,
    course_id,
    activity_id,
    now: datetime | None = None,
) -> None:
    try:
        learning.require_activity_readable(
            db,
            tenant_id=tenant_id,
            person_id=person_id,
            course_id=course_id,
            activity_id=activity_id,
            now=now,
        )
    except learning.PacingForbidden as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc


def require_activity_submittable(
    db: Session,
    *,
    tenant_id,
    person_id,
    course_id,
    activity_id,
    now: datetime | None = None,
) -> None:
    try:
        learning.require_activity_submittable(
            db,
            tenant_id=tenant_id,
            person_id=person_id,
            course_id=course_id,
            activity_id=activity_id,
            now=now,
        )
    except learning.PacingForbidden as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
