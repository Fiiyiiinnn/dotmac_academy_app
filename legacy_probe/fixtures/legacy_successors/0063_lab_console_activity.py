"""Add `lab_console_activity` — console-use heartbeat that keeps a lab alive.

The idle reaper (`lab_jobs.request_idle_reaps`) measured idleness from
`lab_instances.last_active_at`, which only launch, reset and Run checks move,
so a learner typing in a console was reaped after `lab_idle_minutes`.

The web tier deliberately cannot UPDATE `lab_instances` (0056: runtime state is
worker-owned), and this migration keeps that boundary intact. Instead the web
tier records its own observation in this small side table, one row per
instance, and the reaper (a policy reader running as the offline admin role)
takes `greatest(last_active_at, last_seen_at)`. The web role gets exactly:
SELECT, INSERT of (instance_id, tenant_id, last_seen_at) and UPDATE of
(last_seen_at). No DELETE, no other column, and the lab worker and
platform_api get nothing. Row-level security follows the tenant isolation
pattern of the other lab tables, and the composite FK to `lab_instances`
cascades so a deleted instance leaves no row behind.

`last_seen_at` is never trusted from the client: a BEFORE INSERT OR UPDATE
trigger overwrites it with the database clock. Without it the web role could
store a far-future timestamp and keep a lab from ever being reaped (and clock
skew between web hosts could do the same by accident).

Revision ID: 0063_lab_console_activity
Revises: 0062_lab_template_access
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0063_lab_console_activity"
down_revision = "0062_lab_template_access"
branch_labels = None
depends_on = None

TABLE = "lab_console_activity"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("instance_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "instance_id"],
            ["lab_instances.tenant_id", "lab_instances.id"],
            name="fk_lab_console_activity_instance",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_lab_console_activity_tenant_id", TABLE, ["tenant_id"])
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY;")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY;")
    op.execute(
        f"CREATE POLICY {TABLE}_tenant_isolation ON {TABLE} "
        f"USING (tenant_id = app_current_tenant_id()) "
        f"WITH CHECK (tenant_id = app_current_tenant_id());"
    )
    op.execute(
        "CREATE FUNCTION lab_console_activity_stamp() RETURNS trigger "
        "LANGUAGE plpgsql AS $$ BEGIN NEW.last_seen_at := now(); RETURN NEW; END; $$;"
    )
    op.execute(
        f"CREATE TRIGGER {TABLE}_stamp BEFORE INSERT OR UPDATE ON {TABLE} "
        f"FOR EACH ROW EXECUTE FUNCTION lab_console_activity_stamp();"
    )
    op.execute(f"REVOKE ALL PRIVILEGES ON {TABLE} FROM PUBLIC;")
    op.execute(f"GRANT SELECT ON {TABLE} TO app_user;")
    op.execute(f"GRANT INSERT (instance_id, tenant_id, last_seen_at) ON {TABLE} TO app_user;")
    op.execute(f"GRANT UPDATE (last_seen_at) ON {TABLE} TO app_user;")


def downgrade() -> None:
    op.drop_table(TABLE)  # drops the trigger with it
    op.execute("DROP FUNCTION IF EXISTS lab_console_activity_stamp();")
