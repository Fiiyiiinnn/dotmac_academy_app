"""Chapter reading-completion tracking.

Per-learner, per-chapter "marked as read" records so course progress can reflect
reading, not just assessment scores. Tenant-scoped with RLS.
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0024_chapter_reads"; down_revision = "0023_merge_gaps_attempts_heads"
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
    op.create_table("chapter_reads",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("person_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chapter_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("chapters.id", ondelete="CASCADE"), nullable=False),
        *_ts(),
        sa.UniqueConstraint("tenant_id", "person_id", "chapter_id",
                            name="uq_chapter_reads_person_chapter"))
    for c in ("tenant_id", "person_id", "chapter_id"):
        op.create_index(f"ix_chapter_reads_{c}", "chapter_reads", [c])
    _rls("chapter_reads")


def downgrade():
    op.drop_table("chapter_reads")
