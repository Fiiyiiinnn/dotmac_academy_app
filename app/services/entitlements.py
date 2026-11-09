"""Academy course-access signatures and HTTP mapping for public Learning policy."""

# Ported at new-composition genesis (learning-facing wave).

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import dotmac_learning as learning
from dotmac_learning import CourseAccessState as CourseAccessState
from fastapi import HTTPException
from sqlalchemy.orm import Session


def visible_course_ids(db: Session, *, tenant_id: UUID, person_id: UUID) -> set[UUID]:
    return learning.visible_course_ids(db, tenant_id=tenant_id, person_id=person_id)


def course_access_states(db: Session, *, tenant_id: UUID, person_id: UUID) -> dict[UUID, CourseAccessState]:
    return learning.course_access_states(db, tenant_id=tenant_id, person_id=person_id)


def accessible_course_ids(db: Session, *, tenant_id: UUID, person_id: UUID) -> set[UUID]:
    return learning.accessible_course_ids(db, tenant_id=tenant_id, person_id=person_id)


def person_can_access_course(db: Session, *, tenant_id: UUID, person_id: UUID, course_id: UUID) -> bool:
    return learning.person_can_access_course(db, tenant_id=tenant_id, person_id=person_id, course_id=course_id)


def require_course_access(db: Session, *, tenant_id: UUID, person_id: UUID, course_id: UUID) -> None:
    """Retain Academy's HTTP denial while Learning owns the access decision."""
    try:
        learning.require_course_access(db, tenant_id=tenant_id, person_id=person_id, course_id=course_id)
    except learning.CourseAccessDenied as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def open_course_ids(db: Session, *, tenant_id: UUID, person_id: UUID, now: datetime | None = None) -> set[UUID]:
    return learning.open_course_ids(db, tenant_id=tenant_id, person_id=person_id, now=now)


def unmet_prerequisites(db: Session, *, tenant_id: UUID, person_id: UUID, course_id: UUID) -> list[UUID]:
    return learning.unmet_prerequisites(db, tenant_id=tenant_id, person_id=person_id, course_id=course_id)


def require_course_open(
    db: Session,
    *,
    tenant_id: UUID,
    person_id: UUID,
    course_id: UUID,
    now: datetime | None = None,
) -> None:
    """Map the canonical window/prerequisite refusal to the existing HTTP error."""
    try:
        learning.require_course_open(db, tenant_id=tenant_id, person_id=person_id, course_id=course_id, now=now)
    except learning.CourseAccessDenied as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
