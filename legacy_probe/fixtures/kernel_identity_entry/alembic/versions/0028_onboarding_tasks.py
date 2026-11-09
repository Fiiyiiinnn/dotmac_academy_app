"""onboarding_tasks: per-applicant onboarding checklist + RLS

Revision ID: 0028_onboarding_tasks
Revises: 0027_assessment_mode
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0028_onboarding_tasks"
down_revision = "0027_assessment_mode"
branch_labels = None
depends_on = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    ]


def _apply_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")
    op.execute(
        f"CREATE POLICY {table}_tenant_isolation ON {table} "
        f"USING (tenant_id = app_current_tenant_id()) "
        f"WITH CHECK (tenant_id = app_current_tenant_id());"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO app_user, platform_api;")


def upgrade() -> None:
    op.create_table(
        "onboarding_tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("applicant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("key", sa.String(40), nullable=False),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(10), nullable=False, server_default="pending"),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("tenant_id", "applicant_id", "key", name="uq_onboarding_tasks_applicant_key"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "applicant_id"],
            ["applicants.tenant_id", "applicants.id"],
            ondelete="CASCADE",
            name="fk_onboarding_tasks_tenant_applicant",
        ),
    )
    op.create_index("ix_onboarding_tasks_tenant_id", "onboarding_tasks", ["tenant_id"])
    op.create_index("ix_onboarding_tasks_applicant", "onboarding_tasks", ["tenant_id", "applicant_id"])
    _apply_rls("onboarding_tasks")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS onboarding_tasks_tenant_isolation ON onboarding_tasks;")
    op.drop_index("ix_onboarding_tasks_applicant", table_name="onboarding_tasks")
    op.drop_index("ix_onboarding_tasks_tenant_id", table_name="onboarding_tasks")
    op.drop_table("onboarding_tasks")
