"""Academy rendering and notification adapters for public Learning announcements."""

# Ported at new-composition genesis (learning-facing wave).

from __future__ import annotations

from typing import cast
from uuid import UUID

import dotmac_learning as learning
from dotmac_kernel.audit import resolve_event_attribution
from sqlalchemy.orm import Session

from app.services.authoring import render_markdown
from app.services.notifications import notify_many
from app.services.settings_store import effective


def _notify(
    db: Session,
    announcement: learning.AnnouncementFact,
    recipient_ids: tuple[UUID, ...],
) -> None:
    notify_many(
        db,
        tenant_id=announcement.tenant_id,
        person_ids=list(recipient_ids),
        kind="announcement",
        title=announcement.title,
        body="",
        link="/announcements",
    )


def create(
    db: Session,
    *,
    tenant_id: UUID,
    author_person_id: UUID,
    title: str,
    body_md: str,
    cohort_id: UUID | None = None,
) -> learning.AnnouncementFact:
    """Render once before dispatch, using the authenticated caller's author ID."""
    body_html = render_markdown(body_md)
    command = learning.CreateAnnouncementCommand(
        tenant_id=tenant_id,
        author_person_id=author_person_id,
        title=title,
        body_md=body_md,
        body_html=body_html,
        cohort_id=cohort_id,
    )
    audit_context = learning.AttemptAuditContext(
        tenant_id=tenant_id,
        actor_type="user",
        actor_id=str(author_person_id),
        source_application=resolve_event_attribution(None),
    )
    return learning.create_announcement(db, command, audit_context=audit_context, fanout=_notify)


def for_person(
    db: Session,
    *,
    tenant_id: UUID,
    person_id: UUID,
    limit: int | None = None,
) -> list[learning.AnnouncementFact]:
    if limit is None:
        limit = cast(int, effective(db, require_store=True)["learning_person_announcements_limit"])
    return list(learning.announcements_for_person(db, tenant_id=tenant_id, person_id=person_id, limit=limit))


def list_for_tenant(
    db: Session,
    *,
    tenant_id: UUID,
    limit: int | None = None,
) -> list[learning.AnnouncementFact]:
    if limit is None:
        limit = cast(int, effective(db, require_store=True)["learning_tenant_announcements_limit"])
    return list(learning.list_announcements(db, tenant_id=tenant_id, limit=limit))


def delete(
    db: Session,
    *,
    tenant_id: UUID,
    announcement_id: UUID,
) -> None:
    learning.delete_announcement(
        db,
        learning.DeleteAnnouncementCommand(tenant_id=tenant_id, announcement_id=announcement_id),
    )
