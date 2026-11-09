"""Student reminder ledger and per-person delivery preferences.

``ReminderLog`` is the idempotence ledger and delivery history for the
reminder policy service (``app/services/reminders.py`` — the single decision
owner). One row per (person, occurrence_key): the same occurrence is never
sent twice. ``ReminderPreference`` stores the person's frequency mode,
quiet hours (academy-local), and per-event opt-outs.
"""

# Ported at new-composition genesis (delivery-core slice).

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from dotmac_kernel.models import Party, PartyPerson, Tenant
from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, TimestampMixin, uuid_pk

# The full set of reminder event kinds the policy service can emit.
EVENT_KINDS: tuple[str, ...] = (
    "course_assigned",
    "course_starting",
    "due_72h",
    "due_24h",
    "overdue",
    "session_24h",
    "session_1h",
    "inactivity",
    "graded",
    "course_completed",
    "certificate_issued",
)

# ReminderLog.status lifecycle: queued (awaiting digest/quiet-hours window)
# -> sent (handed to outbox/notifications) | skipped (opted out at decision time).
STATUS_QUEUED = "queued"
STATUS_SENT = "sent"
STATUS_SKIPPED = "skipped"


class ReminderLog(Base, TimestampMixin):
    __tablename__ = "reminder_log"
    __table_args__ = (
        UniqueConstraint("tenant_id", "person_id", "occurrence_key", name="uq_reminder_log_occurrence"),
        ForeignKeyConstraint(
            ["tenant_id", "person_id"],
            [Party.__table__.c.tenant_id, Party.__table__.c.id],
            ondelete="CASCADE",
            name="fk_reminder_log_tenant_person",
        ),
        ForeignKeyConstraint(
            ["person_id"], [PartyPerson.__table__.c.party_id], ondelete="CASCADE", name="fk_reminder_log_person"
        ),
    )
    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey(Tenant.__table__.c.id, ondelete="CASCADE"), nullable=False, index=True
    )
    person_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)
    event_kind: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    # Stable identity of the occurrence, e.g. "due_24h:activity:<uuid>".
    occurrence_key: Mapped[str] = mapped_column(String(160), nullable=False)
    title: Mapped[str] = mapped_column(String(240), nullable=False, default="")
    link: Mapped[str | None] = mapped_column(String(300))
    channel: Mapped[str] = mapped_column(String(20), nullable=False, default="email")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=STATUS_QUEUED, index=True)
    # Idempotency key of the outbox row that carried this reminder (email channel).
    outbox_key: Mapped[str | None] = mapped_column(String(200))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReminderPreference(Base, TimestampMixin):
    __tablename__ = "reminder_preferences"
    __table_args__ = (
        UniqueConstraint("tenant_id", "person_id", name="uq_reminder_preferences_person"),
        ForeignKeyConstraint(
            ["tenant_id", "person_id"],
            [Party.__table__.c.tenant_id, Party.__table__.c.id],
            ondelete="CASCADE",
            name="fk_reminder_preferences_tenant_person",
        ),
        ForeignKeyConstraint(
            ["person_id"], [PartyPerson.__table__.c.party_id], ondelete="CASCADE", name="fk_reminder_preferences_person"
        ),
    )
    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey(Tenant.__table__.c.id, ondelete="CASCADE"), nullable=False, index=True
    )
    person_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)
    # immediate | daily_digest | weekly_digest (email channel pacing).
    frequency: Mapped[str] = mapped_column(String(20), nullable=False, default="immediate")
    # Academy-local hours [0..23]; both set => suppress immediate email sends
    # inside the window (deferred to the next sweep outside it). Null = none.
    quiet_start_hour: Mapped[int | None] = mapped_column(Integer)
    quiet_end_hour: Mapped[int | None] = mapped_column(Integer)
    # List of event kinds the person has opted out of entirely (both channels).
    optouts: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
