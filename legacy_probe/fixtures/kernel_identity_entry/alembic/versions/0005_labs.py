from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0005_labs"; down_revision = "0004_assessment"
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
    op.create_table("lab_templates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("course_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chapter_number", sa.Integer(), nullable=True),
        sa.Column("activity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("slug", sa.String(63), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("topology", sa.Text(), nullable=False, server_default=""),
        sa.Column("instructions_html", sa.Text(), nullable=False, server_default=""),
        sa.Column("checks", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("seed_spec", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("limits", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("engine", sa.String(20), nullable=False, server_default="containerlab"),
        sa.Column("source_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        *_ts(),
        sa.UniqueConstraint("tenant_id", "id", name="uq_lab_templates_tenant_id_id"),
        sa.UniqueConstraint("activity_id", name="uq_lab_templates_activity_id"))
    for c in ("tenant_id", "course_id", "activity_id"):
        op.create_index(f"ix_lab_templates_{c}", "lab_templates", [c])

    op.create_table("lab_instances",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("activity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("person_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("instance_name", sa.String(120), nullable=False),
        sa.Column("seed", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("consoles", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_active_at", sa.DateTime(timezone=True), nullable=True),
        *_ts(),
        sa.UniqueConstraint("tenant_id", "id", name="uq_lab_instances_tenant_id_id"))
    for c in ("tenant_id", "activity_id", "person_id"):
        op.create_index(f"ix_lab_instances_{c}", "lab_instances", [c])

    for t in ("lab_templates", "lab_instances"):
        _rls(t)


def downgrade():
    op.drop_table("lab_instances")
    op.drop_table("lab_templates")
