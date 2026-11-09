"""Assessment mode (practice | graded | exam) for attempt/reveal policy.

Existing activities backfill to 'graded' — best-of scoring with answers
withheld until the learner passes or exhausts their attempts. Instructors can
relax individual activities to 'practice' or tighten to 'exam'.
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0027_assessment_mode"; down_revision = "0026_class_sessions"
branch_labels = None; depends_on = None


def upgrade():
    op.add_column(
        "activities",
        sa.Column("assessment_mode", sa.String(10), nullable=False, server_default="graded"),
    )


def downgrade():
    op.drop_column("activities", "assessment_mode")
