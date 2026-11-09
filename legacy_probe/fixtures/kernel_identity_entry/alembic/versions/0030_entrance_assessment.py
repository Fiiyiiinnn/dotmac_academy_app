"""Entrance assessment: cohort-scoped applications + candidate competency profile.

Applicants gain a cohort link and a stored assessment result (overall score,
level band, per-category profile). Cohorts gain a designated entrance bank.
Questions gain an optional skill-domain ``category`` (distinct from the cognitive
``rubric_category``) that the profile groups by.
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0030_entrance_assessment"
down_revision = "0029_completion_erp_synced"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("applicants", sa.Column("cohort_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("applicants", sa.Column("assessment_score", sa.Float(), nullable=True))
    op.add_column("applicants", sa.Column("assessment_level", sa.String(20), nullable=True))
    op.add_column("applicants", sa.Column("assessment_profile", postgresql.JSONB(), nullable=True))
    op.add_column("applicants", sa.Column("assessment_taken_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("cohorts", sa.Column("entrance_bank_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("questions", sa.Column("category", sa.String(40), nullable=True))


def downgrade() -> None:
    op.drop_column("questions", "category")
    op.drop_column("cohorts", "entrance_bank_id")
    op.drop_column("applicants", "assessment_taken_at")
    op.drop_column("applicants", "assessment_profile")
    op.drop_column("applicants", "assessment_level")
    op.drop_column("applicants", "assessment_score")
    op.drop_column("applicants", "cohort_id")
