"""In-app authoring: chapters.body_md (editable markdown source).

Slice 5c / finding #8.
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0019_chapter_body_md"; down_revision = "0018_course_status"
branch_labels = None; depends_on = None


def upgrade():
    op.add_column("chapters", sa.Column("body_md", sa.Text(), nullable=False, server_default=""))


def downgrade():
    op.drop_column("chapters", "body_md")
