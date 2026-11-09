"""Learner chapter bookmarks and private notes (roadmap P1 item 11).

Revision ID: 0043_bookmarks_notes
Revises: 0042_course_listed
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0043_bookmarks_notes"
down_revision = "0042_course_listed"
branch_labels = None
depends_on = None


def _ts():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    ]


def _rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")
    op.execute(
        f"CREATE POLICY {table}_tenant_isolation ON {table} "
        f"USING (tenant_id = app_current_tenant_id()) "
        f"WITH CHECK (tenant_id = app_current_tenant_id());"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO app_user, platform_api;")


def _table(name: str, extra_cols: list) -> None:
    op.create_table(
        name,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("person_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("course_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("courses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("chapter_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("chapters.id", ondelete="CASCADE"), nullable=False),
        *extra_cols,
        *_ts(),
        sa.UniqueConstraint("tenant_id", "person_id", "chapter_id",
                            name=f"uq_{name}_person_chapter"),
    )
    for c in ("tenant_id", "person_id", "course_id", "chapter_id"):
        op.create_index(f"ix_{name}_{c}", name, [c])
    _rls(name)


def upgrade() -> None:
    _table("chapter_bookmarks", [])
    _table("chapter_notes", [sa.Column("body", sa.Text(), nullable=False, server_default="")])


def downgrade() -> None:
    op.drop_table("chapter_notes")
    op.drop_table("chapter_bookmarks")
