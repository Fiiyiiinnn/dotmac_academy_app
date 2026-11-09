"""Add courses.listed — the single flag controlling public-catalog visibility.

Revision ID: 0042_course_listed
Revises: 0041_review_remediation

The public catalog is a projection of the courses table; ``listed`` is its one
canonical selector. Backfill: technical student-facing courses are listed;
internal material (instructor guide, entrance-assessment shell) and the
internal ``management`` discipline stay unlisted.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0042_course_listed"
down_revision = "0041_review_remediation"
branch_labels = None
depends_on = None

_UNLISTED_SLUGS = ("instructor-guide", "fibre-entrance")


def upgrade() -> None:
    op.add_column(
        "courses",
        sa.Column("listed", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.execute(
        sa.text(
            "UPDATE courses SET listed = TRUE"
            " WHERE status = 'published'"
            " AND discipline != 'management'"
            " AND slug NOT IN :slugs"
        ).bindparams(sa.bindparam("slugs", _UNLISTED_SLUGS, expanding=True))
    )


def downgrade() -> None:
    op.drop_column("courses", "listed")
