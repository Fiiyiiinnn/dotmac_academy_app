"""Offering scheduling window: starts_at / ends_at on course_offerings.

Slice 2a. Null on either edge = open-ended; null window = always open, so
existing offerings remain available.
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0009_offering_window"; down_revision = "0008_course_offerings"
branch_labels = None; depends_on = None


def upgrade():
    op.add_column("course_offerings",
                  sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("course_offerings",
                  sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    op.drop_column("course_offerings", "ends_at")
    op.drop_column("course_offerings", "starts_at")
