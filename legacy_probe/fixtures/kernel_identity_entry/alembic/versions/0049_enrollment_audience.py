"""Staff vs external audience on an enrolment, with the ERP employee reference.

Revision ID: 0049_enrollment_audience
Revises: 0048_subtopic_reads
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0049_enrollment_audience"
down_revision = "0048_subtopic_reads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable on purpose, with no default. "Unclassified" is a real, honest
    # state: the Academy cannot read the ERP roster (the integration is an
    # outbound webhook only), so defaulting every existing row to 'external'
    # would manufacture a fact nobody established. ADR 0004 forbids inferring
    # audience from the email domain.
    op.add_column("enrollments", sa.Column("audience", sa.String(length=20), nullable=True))
    op.add_column("enrollments", sa.Column("employee_ref", sa.String(length=64), nullable=True))
    op.create_index("ix_enrollments_audience", "enrollments", ["tenant_id", "audience"])
    op.create_check_constraint(
        "ck_enrollments_audience",
        "enrollments",
        "audience IS NULL OR audience IN ('staff', 'external')",
    )
    # Identity between the systems is the employee reference, never a
    # lowercased email match — so a staff row must actually carry one.
    op.create_check_constraint(
        "ck_enrollments_staff_has_employee_ref",
        "enrollments",
        "audience IS DISTINCT FROM 'staff' OR employee_ref IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint("ck_enrollments_staff_has_employee_ref", "enrollments", type_="check")
    op.drop_constraint("ck_enrollments_audience", "enrollments", type_="check")
    op.drop_index("ix_enrollments_audience", table_name="enrollments")
    op.drop_column("enrollments", "employee_ref")
    op.drop_column("enrollments", "audience")
