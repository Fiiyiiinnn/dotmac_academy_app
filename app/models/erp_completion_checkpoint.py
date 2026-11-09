"""Academy ERP sender markers and immutable imported-completion membership.

Historical a005 preserves raw ACK custody. The sender updates only checkpoint
markers; imported membership belongs exclusively to the guarded migration.
"""

# Ported at new-composition genesis (ERP slice).

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from dotmac_kernel.models import Tenant
from dotmac_learning.models import CourseCompletion
from sqlalchemy import DateTime, Float, ForeignKey, ForeignKeyConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ERPCompletionCheckpoint(Base):
    __tablename__ = "academy_erp_completion_checkpoints"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "completion_id"],
            [CourseCompletion.__table__.c.tenant_id, CourseCompletion.__table__.c.id],
            ondelete="CASCADE",
            name="fk_erp_checkpoint_tenant_completion",
        ),
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(Tenant.__table__.c.id, ondelete="CASCADE"),
        primary_key=True,
    )
    completion_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    erp_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    erp_synced_pct: Mapped[float | None] = mapped_column(Float, nullable=True)


class ERPImportedCompletionKey(Base):
    __tablename__ = "academy_erp_imported_completion_keys"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "completion_id"],
            [CourseCompletion.__table__.c.tenant_id, CourseCompletion.__table__.c.id],
            ondelete="CASCADE",
            name="fk_erp_imported_key_tenant_completion",
        ),
    )
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(Tenant.__table__.c.id, ondelete="CASCADE"),
        primary_key=True,
    )
    completion_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
