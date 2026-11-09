"""Academy calendar labels and links over the public Learning agenda."""

# Ported at new-composition genesis (learning-facing wave).

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import dotmac_learning as learning
from sqlalchemy.orm import Session

from app.services.settings_store import effective


def _item(row: learning.AgendaItem) -> dict:
    item: dict = {"when": row.when, "kind": row.kind, "title": row.title, "course": row.course_title}
    if row.kind in {"opens", "closes"}:
        item["title"] = f"{row.title} {row.kind}"
        item["link"] = f"/courses/{row.target.course_slug}"
    elif row.kind == "due":
        item["link"] = f"/activities/{row.target.id}"
    else:
        session = row.session
        if session is None:
            raise AssertionError("Session agenda item has no session fact.")
        suffix = "" if session.session_type == "live_class" else f" ({session.session_type})"
        item.update(
            title=session.title + suffix,
            course=session.location or "",
            link=f"/timetable/sessions/{row.target.id}",
            join_url=session.join_url,
            joinable=row.joinable,
        )
    return item


def upcoming_for_person(db: Session, *, tenant_id: UUID, person_id: UUID, limit: int | None = None) -> list[dict]:
    """Resolve omitted cap strictly; preserve explicit caller caps including zero."""
    now = datetime.now(UTC)
    policy = effective(db, require_store=True)
    if limit is None:
        limit = cast(int, policy["learning_agenda_limit"])
    rows = learning.upcoming_agenda(
        db,
        tenant_id=tenant_id,
        person_id=person_id,
        limit=limit,
        now=now,
        join_lead_minutes=cast(int, policy["learning_join_lead_minutes"]),
    )
    return [_item(row) for row in rows]
