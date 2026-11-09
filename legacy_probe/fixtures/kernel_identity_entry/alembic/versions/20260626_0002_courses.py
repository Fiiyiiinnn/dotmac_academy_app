from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0002_courses"; down_revision = "0001_initial_tenant_schema"
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
    op.create_table("courses",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("slug", sa.String(63), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("discipline", sa.String(40), nullable=False),
        sa.Column("source_ref", sa.String(120), nullable=False),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        *_ts(),
        sa.UniqueConstraint("tenant_id", "slug", name="uq_courses_tenant_slug"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_courses_tenant_id_id"))
    op.create_index("ix_courses_tenant_id", "courses", ["tenant_id"])
    op.create_table("chapters",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("course_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("number", sa.Integer, nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("part", sa.String(20), nullable=False, server_default=""),
        sa.Column("body_html", sa.Text, nullable=False, server_default=""),
        sa.Column("source_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("order_index", sa.Integer, nullable=False, server_default="0"),
        *_ts(),
        sa.UniqueConstraint("tenant_id", "course_id", "number", name="uq_chapters_tenant_course_number"),
        sa.ForeignKeyConstraint(["tenant_id", "course_id"], ["courses.tenant_id", "courses.id"],
                                ondelete="CASCADE", name="fk_chapters_tenant_course"))
    op.create_index("ix_chapters_tenant_id", "chapters", ["tenant_id"])
    op.create_index("ix_chapters_course_id", "chapters", ["course_id"])
    _rls("courses"); _rls("chapters")

def downgrade():
    op.drop_table("chapters"); op.drop_table("courses")
