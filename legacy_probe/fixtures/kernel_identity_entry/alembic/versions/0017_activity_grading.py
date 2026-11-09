"""Manual grading: activities.grading (auto|manual).

Slice 4b.
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0017_activity_grading"; down_revision = "0016_activity_max_attempts"
branch_labels = None; depends_on = None


def upgrade():
    op.add_column("activities", sa.Column("grading", sa.String(10), nullable=False,
                                          server_default="auto"))


def downgrade():
    op.drop_column("activities", "grading")
