"""admissions: applicants table + RLS

Revision ID: 0025_admissions
Revises: 0024_chapter_reads
Create Date: 2026-07-10
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0025_admissions"
down_revision = "0024_chapter_reads"
branch_labels = None
depends_on = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
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
        "applicants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("first_name", sa.String(80), nullable=False),
        sa.Column("last_name", sa.String(80), nullable=False),
        sa.Column("phone", sa.String(40), nullable=True),
        sa.Column("program", sa.String(120), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="applied"),
        sa.Column("source", sa.String(30), nullable=False, server_default="website"),
        sa.Column("external_ref", sa.String(64), nullable=True),
        sa.Column("notes", sa.String(1000), nullable=True),
        sa.Column("applied_on", sa.Date(), nullable=False, server_default=sa.func.current_date()),
        sa.Column("person_id", postgresql.UUID(as_uuid=True), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("tenant_id", "email", name="uq_applicants_tenant_email"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_applicants_tenant_id_id"),
    )
    op.create_index("ix_applicants_tenant_id", "applicants", ["tenant_id"])
    op.create_index("ix_applicants_status", "applicants", ["tenant_id", "status"])
    _apply_rls("applicants")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS applicants_tenant_isolation ON applicants;")
    op.drop_index("ix_applicants_status", table_name="applicants")
    op.drop_index("ix_applicants_tenant_id", table_name="applicants")
    op.drop_table("applicants")
