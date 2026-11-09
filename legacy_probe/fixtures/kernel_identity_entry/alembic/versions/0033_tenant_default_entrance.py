"""tenants: academy-wide default entrance assessment (applies to all applicants)."""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0033_tenant_default_entrance"
down_revision = "0032_entrance_timer"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("default_entrance_bank_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("tenants", sa.Column("default_entrance_time_limit_minutes", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("tenants", "default_entrance_time_limit_minutes")
    op.drop_column("tenants", "default_entrance_bank_id")
