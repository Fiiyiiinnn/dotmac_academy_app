"""In-app notification service.

All functions are keyword-only after `db`. No db.commit() — callers/get_db own
the transaction boundary. Functions flush so the caller sees the new rows in the
same session.
"""

# Ported at new-composition genesis (identity/facilitator slice).
from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models.notification import Notification


def notify(
    db: Session,
    *,
    tenant_id: UUID,
    person_id: UUID,
    kind: str,
    title: str,
    body: str = "",
    link: str | None = None,
) -> Notification:
    n = Notification(
        tenant_id=tenant_id,
        person_id=person_id,
        kind=kind,
        title=title,
        body=body,
        link=link,
    )
    db.add(n)
    db.flush()
    return n


def notify_many(
    db: Session,
    *,
    tenant_id: UUID,
    person_ids: list[UUID],
    kind: str,
    title: str,
    body: str = "",
    link: str | None = None,
) -> None:
    for pid in person_ids:
        notify(db, tenant_id=tenant_id, person_id=pid, kind=kind, title=title, body=body, link=link)


def unread_count(db: Session, *, tenant_id: UUID, person_id: UUID) -> int:
    result = db.scalar(
        select(func.count())
        .select_from(Notification)
        .where(Notification.tenant_id == tenant_id)
        .where(Notification.person_id == person_id)
        .where(Notification.read_at.is_(None))
    )
    return int(result or 0)


def recent(
    db: Session,
    *,
    tenant_id: UUID,
    person_id: UUID,
    limit: int = 30,
) -> list[Notification]:
    return list(
        db.scalars(
            select(Notification)
            .where(Notification.tenant_id == tenant_id)
            .where(Notification.person_id == person_id)
            .order_by(Notification.created_at.desc())
            .limit(limit)
        ).all()
    )


def mark_all_read(db: Session, *, tenant_id: UUID, person_id: UUID) -> None:
    db.execute(
        update(Notification)
        .where(Notification.tenant_id == tenant_id)
        .where(Notification.person_id == person_id)
        .where(Notification.read_at.is_(None))
        .values(read_at=datetime.now(UTC))
    )
    db.flush()
