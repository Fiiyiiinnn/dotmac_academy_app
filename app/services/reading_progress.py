"""Academy reading-progress signatures over public Learning commands and reads."""

# Ported at new-composition genesis (learning-facing wave).

from __future__ import annotations

import logging
from uuid import UUID

import dotmac_learning as learning
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def completed_slugs(db: Session, *, tenant_id: UUID, person_id: UUID, chapter_id: UUID) -> set[str]:
    return learning.completed_slugs(db, tenant_id=tenant_id, person_id=person_id, chapter_id=chapter_id)


def completed_slugs_by_chapter(
    db: Session, *, tenant_id: UUID, person_id: UUID, chapter_ids: list[UUID]
) -> dict[UUID, set[str]]:
    return learning.completed_slugs_by_chapter(db, tenant_id=tenant_id, person_id=person_id, chapter_ids=chapter_ids)


def mark_complete(
    db: Session,
    *,
    tenant_id: UUID,
    person_id: UUID,
    chapter_id: UUID,
    subtopic_slug: str,
) -> bool:
    """Delegate unmodified input; Learning owns trim/cap/idempotent insertion."""
    return learning.mark_subtopic_complete(
        db,
        learning.SubtopicReadCommand(
            tenant_id=tenant_id,
            person_id=person_id,
            chapter_id=chapter_id,
            subtopic_slug=subtopic_slug,
        ),
    )


def completed_chapter_ids(db: Session, *, tenant_id: UUID, person_id: UUID, chapter_ids: list[UUID]) -> set[UUID]:
    return learning.completed_chapter_ids(db, tenant_id=tenant_id, person_id=person_id, chapter_ids=chapter_ids)


def toggle_chapter_read(db: Session, *, tenant_id: UUID, person_id: UUID, course_id: UUID, chapter_id: UUID) -> bool:
    """Learning owns the toggle, optional completion event and required flushes."""
    outcome = learning.toggle_chapter_read(
        db,
        learning.ChapterReadingCommand(
            tenant_id=tenant_id,
            person_id=person_id,
            course_id=course_id,
            chapter_id=chapter_id,
        ),
        local_day_zone=None,
    )
    if outcome.event is not None and outcome.event.status == "failed":
        logger.warning("learning-event emission failed (%s)", learning.KIND_CHAPTER_COMPLETED)
    return outcome.completed
