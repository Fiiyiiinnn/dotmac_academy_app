"""ERP applicant assessment registration and result delivery state.

Revision ID: 0052_erp_applicant_assessments
Revises: 0051_attempt_grants
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0052_erp_applicant_assessments"
down_revision = "0051_attempt_grants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "applicants",
        sa.Column("assessment_bank_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "applicants",
        sa.Column("assessment_return_url", sa.String(length=2048), nullable=True),
    )
    op.add_column(
        "applicants",
        sa.Column("assessment_erp_synced_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "applicants",
        sa.Column("assessment_result_version", sa.Integer(), nullable=False, server_default="0"),
    )
    op.execute(
        "UPDATE applicants SET assessment_result_version = 1 "
        "WHERE assessment_taken_at IS NOT NULL"
    )
    op.create_foreign_key(
        "fk_applicants_tenant_assessment_bank",
        "applicants",
        "question_banks",
        ["tenant_id", "assessment_bank_id"],
        ["tenant_id", "id"],
    )
    op.create_index("ix_applicants_assessment_bank_id", "applicants", ["assessment_bank_id"])

    op.drop_constraint("uq_applicants_tenant_email", "applicants", type_="unique")
    op.create_index(
        "uq_applicants_tenant_local_email",
        "applicants",
        ["tenant_id", "email"],
        unique=True,
        postgresql_where=sa.text("external_ref IS NULL"),
    )
    op.create_index(
        "uq_applicants_tenant_external_ref",
        "applicants",
        ["tenant_id", "external_ref"],
        unique=True,
        postgresql_where=sa.text("external_ref IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_applicants_tenant_external_ref", table_name="applicants")
    op.drop_index("uq_applicants_tenant_local_email", table_name="applicants")
    op.create_unique_constraint(
        "uq_applicants_tenant_email", "applicants", ["tenant_id", "email"]
    )
    op.drop_index("ix_applicants_assessment_bank_id", table_name="applicants")
    op.drop_constraint(
        "fk_applicants_tenant_assessment_bank", "applicants", type_="foreignkey"
    )
    op.drop_column("applicants", "assessment_result_version")
    op.drop_column("applicants", "assessment_erp_synced_at")
    op.drop_column("applicants", "assessment_return_url")
    op.drop_column("applicants", "assessment_bank_id")
