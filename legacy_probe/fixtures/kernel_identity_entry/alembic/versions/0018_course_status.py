"""Course authoring lifecycle: courses.status (draft|published).

Slice 5a / finding #8. Existing courses default to 'published'.
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0018_course_status"; down_revision = "0017_activity_grading"
branch_labels = None; depends_on = None


def upgrade():
    op.add_column("courses", sa.Column("status", sa.String(20), nullable=False,
                                       server_default="published"))


def downgrade():
    op.drop_column("courses", "status")
