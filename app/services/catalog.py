"""Academy catalog containers backed by public Learning projections."""

# Ported at new-composition genesis (learning-facing wave).

from __future__ import annotations

from typing import Protocol
from uuid import UUID

import dotmac_learning as learning
from sqlalchemy.orm import Session


class CourseIdentity(Protocol):
    """The landing route supplies a course; only its identity crosses the boundary."""

    @property
    def id(self) -> UUID: ...


def my_courses(db: Session, *, tenant_id: UUID, person_id: UUID) -> list[learning.CourseFact]:
    """Courses the person is entitled to see, in the owner's order."""
    return list(learning.my_courses(db, tenant_id=tenant_id, person_id=person_id))


def public_catalog(db: Session, *, tenant_id: UUID) -> list[dict]:
    """Listed published courses in the legacy anonymous catalog containers."""
    return [
        {"course": item.course, "chapter_count": item.chapter_count}
        for item in learning.public_catalog(db, tenant_id=tenant_id)
    ]


def all_courses(db: Session, *, tenant_id: UUID) -> list[learning.CourseFact]:
    """Every tenant course, in the owner's order."""
    return list(learning.all_courses(db, tenant_id=tenant_id))


def course_completion(db: Session, *, tenant_id: UUID, person_id: UUID, course_id: UUID) -> int:
    """Return the owner's integer completion percentage."""
    return learning.course_completion(db, tenant_id=tenant_id, person_id=person_id, course_id=course_id)


def _activity(item: learning.CatalogActivity) -> dict:
    return {"activity": item.activity, "passed": item.passed, "pct": item.pct}


def course_structure(db: Session, *, tenant_id: UUID, person_id: UUID, course: CourseIdentity) -> dict:
    """Adapt grouped facts without changing domain decisions or fact identities."""
    structure = learning.course_structure(db, tenant_id=tenant_id, person_id=person_id, course_id=course.id)
    return {
        "parts": [
            {
                "part": part.part,
                "chapters": [
                    {
                        "chapter": chapter.chapter,
                        "activities": [_activity(item) for item in chapter.activities],
                    }
                    for chapter in part.chapters
                ],
            }
            for part in structure.parts
        ],
        "course_activities": [_activity(item) for item in structure.course_activities],
        "continue_target": structure.continue_target,
        "locked": structure.locked,
    }


def catalog_course_by_slug(db: Session, *, tenant_id: UUID, slug: str) -> learning.CourseFact | None:
    """Indexed owner lookup; route applies current entitlement policy separately."""
    return learning.catalog_course_by_slug(db, tenant_id=tenant_id, slug=slug)


def catalog_chapter_by_number(
    db: Session, *, tenant_id: UUID, course_id: UUID, number: int
) -> learning.ChapterFact | None:
    """One course-bound chapter fact in the caller's transaction."""
    return learning.catalog_chapter_by_number(db, tenant_id=tenant_id, course_id=course_id, number=number)
