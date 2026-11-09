"""Course prerequisites.

Slice 2e. A course may require other courses to be completed first.
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0013_course_prerequisites"; down_revision = "0012_offering_activities"
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
    op.create_table("course_prerequisites",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("course_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("requires_course_id", postgresql.UUID(as_uuid=True), nullable=False),
        *_ts(),
        sa.UniqueConstraint("tenant_id", "id", name="uq_course_prerequisites_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "course_id", "requires_course_id",
                            name="uq_course_prerequisites_pair"),
        sa.ForeignKeyConstraint(["tenant_id", "course_id"],
                                ["courses.tenant_id", "courses.id"],
                                ondelete="CASCADE", name="fk_course_prerequisites_tenant_course"),
        sa.ForeignKeyConstraint(["tenant_id", "requires_course_id"],
                                ["courses.tenant_id", "courses.id"],
                                ondelete="CASCADE", name="fk_course_prerequisites_tenant_requires"))
    for c in ("tenant_id", "course_id", "requires_course_id"):
        op.create_index(f"ix_course_prerequisites_{c}", "course_prerequisites", [c])
    _rls("course_prerequisites")


def downgrade():
    op.drop_table("course_prerequisites")
