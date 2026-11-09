"""Canonical learning-event ledger (roadmap P3a).

Revision ID: 0045_learning_events
Revises: 0044_reminders

Append-only by construction: app_user/platform_api receive SELECT + INSERT
only (deliberate deviation from the house all-privileges grant — the ledger
has no update or delete paths anywhere).
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0045_learning_events"
down_revision = "0044_reminders"
branch_labels = None
depends_on = None

TABLE = "learning_events"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("person_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("course_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("subject_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("detail", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id", "person_id"],
                                ["people.tenant_id", "people.id"],
                                ondelete="CASCADE",
                                name="fk_learning_events_tenant_person"),
    )
    op.create_index("ix_learning_events_tenant_id", TABLE, ["tenant_id"])
    op.create_index("ix_learning_events_person_id", TABLE, ["person_id"])
    op.create_index("ix_learning_events_person_time", TABLE, ["tenant_id", "person_id", "occurred_at"])
    op.create_index("ix_learning_events_course_kind", TABLE, ["tenant_id", "course_id", "kind"])
    op.create_index("ix_learning_events_kind_time", TABLE, ["tenant_id", "kind", "occurred_at"])

    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY;")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY;")
    op.execute(
        f"CREATE POLICY {TABLE}_tenant_isolation ON {TABLE} "
        f"USING (tenant_id = app_current_tenant_id()) "
        f"WITH CHECK (tenant_id = app_current_tenant_id());"
    )
    # Append-only: no UPDATE/DELETE grant on purpose.
    op.execute(f"GRANT SELECT, INSERT ON {TABLE} TO app_user, platform_api;")


def downgrade() -> None:
    op.drop_table(TABLE)
