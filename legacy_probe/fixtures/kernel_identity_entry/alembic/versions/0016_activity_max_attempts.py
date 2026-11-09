"""Assessment attempt limits: activities.max_attempts.

Slice 4a. Null = unlimited.
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0016_activity_max_attempts"; down_revision = "0015_person_status"
branch_labels = None; depends_on = None


def upgrade():
    op.add_column("activities", sa.Column("max_attempts", sa.Integer(), nullable=True))


def downgrade():
    op.drop_column("activities", "max_attempts")
