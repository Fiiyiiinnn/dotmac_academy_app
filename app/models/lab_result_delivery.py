"""PII-free, event-time lab delivery intents owned by Academy."""

# Ported at new-composition genesis; table created by a0001.

from datetime import datetime
from uuid import UUID

from dotmac_kernel.models import Tenant
from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, String, func, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class LabResultDelivery(Base):
    __tablename__ = "lab_result_deliveries"
    __table_args__ = (
        CheckConstraint("status IN ('pending','delivered','failed')", name="lab_result_deliveries_status_check"),
        Index("ix_lab_result_deliveries_status_created", "status", "created_at"),
        Index(
            "ix_lab_result_deliveries_due",
            "next_attempt_at",
            "created_at",
            postgresql_where=text("status = 'pending'"),
        ),
    )

    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(Tenant.__table__.c.id, ondelete="CASCADE", name="lab_result_deliveries_tenant_id_fkey"),
        primary_key=True,
    )
    operation_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    instance_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    person_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    activity_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    course_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    submission_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    score_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    certificate_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    first_completion: Mapped[bool] = mapped_column(Boolean, nullable=False)
    certificate_created: Mapped[bool] = mapped_column(Boolean, nullable=False)
    first_pass: Mapped[bool] = mapped_column(Boolean, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    max_score: Mapped[float] = mapped_column(Float, nullable=False)
    fraction: Mapped[float] = mapped_column(Float, nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_error: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
