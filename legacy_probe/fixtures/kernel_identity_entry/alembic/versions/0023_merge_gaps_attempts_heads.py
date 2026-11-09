"""merge gaps + activity-attempts heads.

Joins the lms-gaps chain (…0021_announcements -> 0022_activity_weight) with the
lms-buildout chain (0019_chapter_body_md -> 0020_activity_attempts). No schema
change: both Activity columns (weight, question_count) ship in their respective
parent revisions; this only reconciles the divergent alembic history.
"""
from __future__ import annotations

revision = "0023_merge_gaps_attempts_heads"
down_revision = ("0022_activity_weight", "0020_activity_attempts")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
