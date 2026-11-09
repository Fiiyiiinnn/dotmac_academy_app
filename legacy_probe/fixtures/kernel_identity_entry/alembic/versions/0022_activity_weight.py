"""Activity weight column for gradebook."""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0022_activity_weight"; down_revision = "0021_announcements"
branch_labels = None; depends_on = None


def upgrade():
    op.add_column("activities", sa.Column("weight", sa.Float(), nullable=False, server_default="1.0"))


def downgrade():
    op.drop_column("activities", "weight")
