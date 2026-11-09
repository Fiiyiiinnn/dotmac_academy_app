"""Student reminder ledger + preferences (roadmap P2).

Revision ID: 0044_reminders
Revises: 0043_bookmarks_notes
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0044_reminders"
down_revision = "0043_bookmarks_notes"
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


def upgrade() -> None:
    op.create_table(
        "reminder_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("person_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_kind", sa.String(30), nullable=False),
        sa.Column("occurrence_key", sa.String(160), nullable=False),
        sa.Column("title", sa.String(240), nullable=False, server_default=""),
        sa.Column("link", sa.String(300), nullable=True),
        sa.Column("channel", sa.String(20), nullable=False, server_default="email"),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("outbox_key", sa.String(200), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        *_ts(),
        sa.UniqueConstraint("tenant_id", "person_id", "occurrence_key",
                            name="uq_reminder_log_occurrence"),
        sa.ForeignKeyConstraint(["tenant_id", "person_id"],
                                ["people.tenant_id", "people.id"],
                                ondelete="CASCADE",
                                name="fk_reminder_log_tenant_person"),
    )
    for c in ("tenant_id", "person_id", "event_kind", "status"):
        op.create_index(f"ix_reminder_log_{c}", "reminder_log", [c])
    _rls("reminder_log")

    op.create_table(
        "reminder_preferences",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("person_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("frequency", sa.String(20), nullable=False, server_default="immediate"),
        sa.Column("quiet_start_hour", sa.Integer(), nullable=True),
        sa.Column("quiet_end_hour", sa.Integer(), nullable=True),
        sa.Column("optouts", postgresql.JSONB(), nullable=False, server_default="[]"),
        *_ts(),
        sa.UniqueConstraint("tenant_id", "person_id",
                            name="uq_reminder_preferences_person"),
        sa.ForeignKeyConstraint(["tenant_id", "person_id"],
                                ["people.tenant_id", "people.id"],
                                ondelete="CASCADE",
                                name="fk_reminder_preferences_tenant_person"),
    )
    for c in ("tenant_id", "person_id"):
        op.create_index(f"ix_reminder_preferences_{c}", "reminder_preferences", [c])
    _rls("reminder_preferences")


def downgrade() -> None:
    op.drop_table("reminder_preferences")
    op.drop_table("reminder_log")
