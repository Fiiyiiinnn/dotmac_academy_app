"""Per-offering activity pacing: release_at / due_at overrides.

Slice 2b.
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0012_offering_activities"; down_revision = "0011_certificates"
branch_labels = None; depends_on = None


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
    op.create_table("offering_activities",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("offering_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("activity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("release_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        *_ts(),
        sa.UniqueConstraint("tenant_id", "id", name="uq_offering_activities_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "offering_id", "activity_id",
                            name="uq_offering_activities_offering_activity"),
        sa.ForeignKeyConstraint(["tenant_id", "offering_id"],
                                ["course_offerings.tenant_id", "course_offerings.id"],
                                ondelete="CASCADE", name="fk_offering_activities_tenant_offering"),
        sa.ForeignKeyConstraint(["tenant_id", "activity_id"],
                                ["activities.tenant_id", "activities.id"],
                                ondelete="CASCADE", name="fk_offering_activities_tenant_activity"))
    for c in ("tenant_id", "offering_id", "activity_id"):
        op.create_index(f"ix_offering_activities_{c}", "offering_activities", [c])
    _rls("offering_activities")


def downgrade():
    op.drop_table("offering_activities")
