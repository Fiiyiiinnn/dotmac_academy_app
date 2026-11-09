"""Explainable Success Queue (roadmap P3b, item 21).

Revision ID: 0046_success_queue
Revises: 0045_learning_events

Replaces the boolean at-risk signal with owned intervention entries: reason,
supporting facts, severity, freshness, assignment, and audited lifecycle.
House RLS pattern; full CRUD grants (entries are managed rows, unlike the
append-only learning-event ledger).
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0046_success_queue"
down_revision = "0045_learning_events"
branch_labels = None
depends_on = None

TABLE = "success_queue_entries"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("person_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("cohort_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reason_kind", sa.String(30), nullable=False),
        sa.Column("supporting_facts", postgresql.JSONB, nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("severity", sa.String(10), nullable=False, server_default="medium"),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("assigned_to", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("recommended_action", sa.Text, nullable=False, server_default=""),
        sa.Column("status", sa.String(15), nullable=False, server_default="open"),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("score_hint", sa.Float, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id", "person_id"],
                                ["people.tenant_id", "people.id"],
                                ondelete="CASCADE",
                                name="fk_success_queue_tenant_person"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_success_queue_tenant_id_id"),
    )
    op.create_index("ix_success_queue_tenant_id", TABLE, ["tenant_id"])
    op.create_index("ix_success_queue_person_id", TABLE, ["person_id"])
    op.create_index("ix_success_queue_cohort_id", TABLE, ["cohort_id"])
    op.create_index("ix_success_queue_status", TABLE, ["tenant_id", "status", "severity"])
    # One OPEN entry per person+reason; acknowledged/resolved history may repeat.
    op.create_index(
        "uq_success_queue_open_person_reason",
        TABLE,
        ["tenant_id", "person_id", "reason_kind"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )

    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY;")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY;")
    op.execute(
        f"CREATE POLICY {TABLE}_tenant_isolation ON {TABLE} "
        f"USING (tenant_id = app_current_tenant_id()) "
        f"WITH CHECK (tenant_id = app_current_tenant_id());"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {TABLE} TO app_user, platform_api;")


def downgrade() -> None:
    op.drop_table(TABLE)
