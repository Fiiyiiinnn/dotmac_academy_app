"""Onboarding tasks — the checklist an accepted applicant completes before enrolling.

When an applicant moves to ``onboarding`` a default set of tasks is seeded
(see ``app/services/onboarding.py``). Enrolment is gated on every task being
done, so the pipeline ``accepted -> onboarding -> enrolled`` has real substance
between the last two steps.
"""

# Ported at new-composition genesis (a0002 lineage); table shapes unchanged.

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from dotmac_kernel.models import Tenant
from sqlalchemy import (
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, TimestampMixin, uuid_pk

# pending | done
TASK_STATUSES = ("pending", "done")


class OnboardingTask(Base, TimestampMixin):
    __tablename__ = "onboarding_tasks"
    __table_args__ = (
        # One row per (applicant, task key).
        UniqueConstraint("tenant_id", "applicant_id", "key", name="uq_onboarding_tasks_applicant_key"),
        # tenant-consistent composite FK to the owning applicant
        ForeignKeyConstraint(
            ["tenant_id", "applicant_id"],
            ["applicants.tenant_id", "applicants.id"],
            ondelete="CASCADE",
            name="fk_onboarding_tasks_tenant_applicant",
        ),
        Index("ix_onboarding_tasks_applicant", "tenant_id", "applicant_id"),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(Tenant.__table__.c.id, ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    applicant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)

    # Stable machine key (e.g. "entrance_assessment") + human label.
    key: Mapped[str] = mapped_column(String(40), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default="pending")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
