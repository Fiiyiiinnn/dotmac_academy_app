"""Durable per-learner subtopic progress (was browser localStorage).

Revision ID: 0048_subtopic_reads
Revises: 0047_learning_event_dedup
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0048_subtopic_reads"
down_revision = "0047_learning_event_dedup"
branch_labels = None
depends_on = None

TABLE = "subtopic_reads"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("person_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chapter_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("chapters.id", ondelete="CASCADE"), nullable=False),
        sa.Column("subtopic_slug", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "person_id", "chapter_id", "subtopic_slug",
                            name="uq_subtopic_reads_person_subtopic"),
    )
    for c in ("tenant_id", "person_id", "chapter_id"):
        op.create_index(f"ix_{TABLE}_{c}", TABLE, [c])

    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY;")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY;")
    op.execute(
        f"CREATE POLICY {TABLE}_tenant_isolation ON {TABLE} "
        f"USING (tenant_id = app_current_tenant_id()) "
        f"WITH CHECK (tenant_id = app_current_tenant_id());"
    )
    # No DELETE: completing a subtopic is a one-way observation, mirroring the
    # append-only intent of the learning-event ledger. A learner re-reading a
    # section does not un-complete it, and nothing else should erase the record.
    op.execute(f"GRANT SELECT, INSERT ON {TABLE} TO app_user, platform_api;")


def downgrade() -> None:
    op.drop_table(TABLE)
