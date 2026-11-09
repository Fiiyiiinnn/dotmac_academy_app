"""scheduling: class_sessions table + cohort delivery_mode

Revision ID: 0026_class_sessions
Revises: 0025_admissions
Create Date: 2026-07-11
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0026_class_sessions"
down_revision = "0025_admissions"
branch_labels = None
depends_on = None


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
    op.add_column(
        "cohorts",
        sa.Column("delivery_mode", sa.String(20), nullable=False, server_default="self_paced"),
    )

    op.create_table(
        "class_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("cohort_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("offering_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("session_type", sa.String(20), nullable=False, server_default="live_class"),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("instructor_person_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("location", sa.String(160), nullable=True),
        sa.Column("join_url", sa.String(500), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="scheduled"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "id", name="uq_class_sessions_tenant_id_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "cohort_id"],
            ["cohorts.tenant_id", "cohorts.id"],
            ondelete="CASCADE",
            name="fk_class_sessions_tenant_cohort",
        ),
    )
    op.create_index("ix_class_sessions_tenant_id", "class_sessions", ["tenant_id"])
    op.create_index("ix_class_sessions_cohort_start", "class_sessions", ["tenant_id", "cohort_id", "starts_at"])
    op.create_index("ix_class_sessions_instructor", "class_sessions", ["tenant_id", "instructor_person_id"])
    _apply_rls("class_sessions")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS class_sessions_tenant_isolation ON class_sessions;")
    op.drop_table("class_sessions")
    op.drop_column("cohorts", "delivery_mode")
