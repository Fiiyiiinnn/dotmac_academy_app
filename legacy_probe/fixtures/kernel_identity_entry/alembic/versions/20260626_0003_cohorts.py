from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0003_cohorts"; down_revision = "0002_courses"
branch_labels = None; depends_on = None
from sqlalchemy.dialects import postgresql


def _ts(): return [
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())]


def _rls(table):
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")
    op.execute(f"CREATE POLICY {table}_tenant_isolation ON {table} "
               f"USING (tenant_id = app_current_tenant_id()) "
               f"WITH CHECK (tenant_id = app_current_tenant_id());")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO app_user, platform_api;")


def upgrade():
    op.create_table("cohorts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("discipline", sa.String(40), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        *_ts(), sa.UniqueConstraint("tenant_id", "id", name="uq_cohorts_tenant_id_id"))
    op.create_index("ix_cohorts_tenant_id", "cohorts", ["tenant_id"])
    op.create_table("enrollments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("cohort_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("person_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role_in_cohort", sa.String(20), nullable=False, server_default="student"),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        *_ts(),
        sa.UniqueConstraint("tenant_id", "cohort_id", "person_id", name="uq_enrollments_member"),
        sa.ForeignKeyConstraint(["tenant_id", "cohort_id"], ["cohorts.tenant_id", "cohorts.id"],
                                ondelete="CASCADE", name="fk_enrollments_tenant_cohort"),
        sa.ForeignKeyConstraint(["tenant_id", "person_id"], ["people.tenant_id", "people.id"],
                                ondelete="CASCADE", name="fk_enrollments_tenant_person"))
    for c in ("tenant_id", "cohort_id", "person_id"):
        op.create_index(f"ix_enrollments_{c}", "enrollments", [c])
    _rls("cohorts"); _rls("enrollments")


def downgrade():
    op.drop_table("enrollments"); op.drop_table("cohorts")
