"""Academy annotation signatures and legacy containers over public Learning facts."""

# Ported at new-composition genesis (learning-facing wave 2).

from __future__ import annotations

from uuid import UUID

import dotmac_learning as learning
from sqlalchemy.orm import Session

from app.services.entitlements import visible_course_ids


def get_state(db: Session, *, tenant_id: UUID, person_id: UUID, chapter_id: UUID) -> dict:
    """The learner's bookmark/note state for one chapter."""
    state = learning.annotation_state(db, tenant_id=tenant_id, person_id=person_id, chapter_id=chapter_id)
    return {"bookmarked": state.bookmarked, "note": state.note}


def toggle_bookmark(db: Session, *, tenant_id: UUID, person_id: UUID, course_id: UUID, chapter_id: UUID) -> bool:
    """Delegate the toggle in the same caller transaction."""
    return learning.toggle_bookmark(
        db,
        learning.AnnotationCommand(
            tenant_id=tenant_id,
            person_id=person_id,
            course_id=course_id,
            chapter_id=chapter_id,
        ),
    )


def save_note(
    db: Session,
    *,
    tenant_id: UUID,
    person_id: UUID,
    course_id: UUID,
    chapter_id: UUID,
    body: str,
) -> learning.NoteFact | None:
    """Delegate raw note text; Learning owns trimming and blank deletion."""
    return learning.save_note(
        db,
        learning.NoteCommand(
            tenant_id=tenant_id,
            person_id=person_id,
            course_id=course_id,
            chapter_id=chapter_id,
            body=body,
        ),
    )


def list_for_person(db: Session, *, tenant_id: UUID, person_id: UUID) -> list[dict]:
    """Return the existing grouped containers with detached public fact leaves."""
    course_ids = visible_course_ids(db, tenant_id=tenant_id, person_id=person_id)
    if not course_ids:
        return []
    groups = learning.list_annotations(
        db,
        tenant_id=tenant_id,
        person_id=person_id,
        visible_course_ids=tuple(course_ids),
    )
    return [
        {
            "course": group.course,
            "chapters": [
                {
                    "chapter": item.chapter,
                    "bookmarked": item.bookmarked,
                    "note": item.note,
                }
                for item in group.chapters
            ],
        }
        for group in groups
    ]
