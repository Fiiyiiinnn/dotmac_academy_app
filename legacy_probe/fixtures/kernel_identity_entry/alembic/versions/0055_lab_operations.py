"""Add `lab_operations` — the durable work queue a lab worker will claim from.

Phase 1 of the lab-worker-ownership design (Knowledge slug
`academy-lab-worker-ownership-design-2026-09-18`): schema only. Nothing reads
or writes this table yet. `kind` and `state` are deliberately left without a
DB CHECK constraint — the enum of valid values is still settling and a later
migration will add the constraint once a worker actually exists to honor it.
`requested_by` is deliberately not a declared FK to `people` yet, for the same
reason.

The load-bearing invariant of this whole design lives in the grants, not the
columns, and it is narrower than "no UPDATE/DELETE": `app_user` gets
table-wide SELECT plus a *column-level* INSERT grant covering only
`id, tenant_id, instance_id, kind, requested_by` — the columns a request
actually needs to set. `state`, `attempts`, and every claim/settlement column
are left out of that grant entirely, so `app_user` cannot forge worker-owned
state (e.g. `state='claimed'`) at INSERT time either — denying UPDATE alone
would only stop changing an existing row, not establishing a false one at
creation. `platform_api` keeps table-wide SELECT only. `app_admin` is granted
full SELECT/INSERT/UPDATE/DELETE explicitly: in production `app_admin` runs
the migration and is already the table owner, but CI runs migrations as the
`postgres` superuser (see `.github/workflows/ci.yml`), which owns the table
there instead — `GRANT ALL ON SCHEMA public TO app_admin` in
`scripts/initdb-roles.sql` is schema-level (USAGE/CREATE) and does not cascade
into table ACLs, and `BYPASSRLS` bypasses row-visibility policies only, not
the base grant system. The dedicated non-owner worker therefore also receives
an explicit runtime grant below; app_admin's grant is retained for migrations
and offline maintenance.

Because `state`/`attempts` are excluded from `app_user`'s INSERT grant, the
ORM model (see `app/models/lab.py`) declares `server_default=` for both,
matching this migration's DB-level defaults exactly, and carries no
*client-side* `default=` for either. A client-side `default=` makes
SQLAlchemy mention the column explicitly in the compiled INSERT even when the
caller never set it, which would hit the missing column privilege on every
plain insert. Declaring neither kind of default is just as broken the other
way: SQLAlchemy's ORM unit-of-work still sends every mapped column explicitly
(as `NULL`) unless it is told a server-side default exists — failing the NOT
NULL constraint outright. `server_default=` is what makes the ORM omit the
column and refresh the object from the row Postgres actually inserted.

The partial unique index `uq_lab_operations_open_per_instance` is the other
half: at most one `queued` or `claimed` operation may exist per instance at a
time, so a second deploy/destroy/check request for the same instance is
rejected at the database rather than racing an in-flight one.

Revision ID: 0055_lab_operations
Revises: 0054_lab_instructions_md
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0055_lab_operations"
down_revision = "0054_lab_instructions_md"
branch_labels = None
depends_on = None

TABLE = "lab_operations"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("instance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("requested_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("claimed_by", sa.String(200), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id", "instance_id"],
                                ["lab_instances.tenant_id", "lab_instances.id"],
                                ondelete="CASCADE",
                                name="fk_lab_operations_tenant_instance"),
    )
    op.create_index(
        "uq_lab_operations_open_per_instance",
        TABLE,
        ["instance_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('queued', 'claimed')"),
    )
    op.create_index("ix_lab_operations_claim", TABLE, ["state", "not_before", "requested_at"])
    op.create_index("ix_lab_operations_tenant_id", TABLE, ["tenant_id"])

    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY;")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY;")
    op.execute(
        f"CREATE POLICY {TABLE}_tenant_isolation ON {TABLE} "
        f"USING (tenant_id = app_current_tenant_id()) "
        f"WITH CHECK (tenant_id = app_current_tenant_id());"
    )
    # app_user may SELECT any column but INSERT only the "request" columns —
    # id (client-generated), tenant_id, instance_id, kind, requested_by. Every
    # worker-owned column (state, claimed_by, claimed_at, heartbeat_at,
    # attempts, finished_at, last_error) is left out of the INSERT grant
    # entirely, so app_user cannot forge worker-owned state at row creation,
    # not just after it via UPDATE.
    op.execute(f"GRANT SELECT ON {TABLE} TO app_user;")
    op.execute(f"GRANT INSERT (id, tenant_id, instance_id, kind, requested_by) "
               f"ON {TABLE} TO app_user;")
    op.execute(f"GRANT SELECT ON {TABLE} TO platform_api;")
    # Explicit even though app_admin is the table owner in production — CI
    # migrates as the `postgres` superuser, which owns the table there instead,
    # and app_admin would otherwise have zero privileges on it in CI. See the
    # module docstring for the full explanation.
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {TABLE} TO app_admin;")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON {TABLE} TO academy_lab_worker;")


def downgrade() -> None:
    op.drop_table(TABLE)
