"""Random question pools: activities.question_count + activity_attempts.

Slice 4e. question_count null = show the whole bank (current behaviour).
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0020_activity_attempts"; down_revision = "0019_chapter_body_md"
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
    op.add_column("activities", sa.Column("question_count", sa.Integer(), nullable=True))
    op.create_table("activity_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("activity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("person_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("question_ext_ids", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'[]'::jsonb")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        *_ts(),
        sa.UniqueConstraint("tenant_id", "id", name="uq_activity_attempts_tenant_id_id"),
        sa.ForeignKeyConstraint(["tenant_id", "activity_id"],
                                ["activities.tenant_id", "activities.id"],
                                ondelete="CASCADE", name="fk_activity_attempts_tenant_activity"))
    for c in ("tenant_id", "activity_id", "person_id"):
        op.create_index(f"ix_activity_attempts_{c}", "activity_attempts", [c])
    _rls("activity_attempts")


def downgrade():
    op.drop_table("activity_attempts")
    op.drop_column("activities", "question_count")
