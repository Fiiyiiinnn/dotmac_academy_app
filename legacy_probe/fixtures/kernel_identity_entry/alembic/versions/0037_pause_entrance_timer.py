"""Pause entrance timer while disconnected.

Revision ID: 0037_pause_entrance_timer
Revises: 0035_applicant_profile
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0037_pause_entrance_timer"
down_revision = "0035_applicant_profile"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "applicants",
        sa.Column("assessment_elapsed_seconds", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("applicants", "assessment_elapsed_seconds")
