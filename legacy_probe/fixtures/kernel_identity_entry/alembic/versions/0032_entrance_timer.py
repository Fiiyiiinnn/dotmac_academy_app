"""Time-bound entrance assessment: per-cohort limit + per-applicant start/overrun."""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0032_entrance_timer"
down_revision = "0031_applicant_assessment_token"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("cohorts", sa.Column("entrance_time_limit_minutes", sa.Integer(), nullable=True))
    op.add_column("applicants", sa.Column("assessment_started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "applicants",
        sa.Column("assessment_time_exceeded", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("applicants", "assessment_time_exceeded")
    op.drop_column("applicants", "assessment_started_at")
    op.drop_column("cohorts", "entrance_time_limit_minutes")
