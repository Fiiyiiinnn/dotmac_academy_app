"""cohorts.auto_accept_threshold — entrance-score fraction that auto-accepts."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0039_auto_accept_threshold"
down_revision = "0038_onboarding_token"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("cohorts", sa.Column("auto_accept_threshold", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("cohorts", "auto_accept_threshold")
