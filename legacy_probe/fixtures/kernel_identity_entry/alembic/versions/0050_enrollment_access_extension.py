"""Per-learner enrollment access extension.

Revision ID: 0050_enrollment_access_extension
Revises: 0049_enrollment_audience
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0050_enrollment_access_extension"
down_revision = "0049_enrollment_audience"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("enrollments", sa.Column("access_ends_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_enrollments_access_ends_at", "enrollments", ["tenant_id", "access_ends_at"])


def downgrade() -> None:
    op.drop_index("ix_enrollments_access_ends_at", table_name="enrollments")
    op.drop_column("enrollments", "access_ends_at")
