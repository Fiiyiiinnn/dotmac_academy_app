"""Add a global unique index on `lab_instances.instance_name`.

`instance_name` is generated in `app/services/lab_lifecycle.py`'s
`request_lab()`. Prior to this migration it was derived from a
`COUNT(*)`-per-(tenant, person, activity) sequence number
(`dal-<t8>-<p8>-<a8>-<n>`), which is not collision-proof if a row is ever
hard-deleted — nothing in the application does this today, so this is
defense-in-depth schema hardening, not a fix for an active bug. Going
forward, `request_lab()` allocates the instance's own UUID first and derives
its name directly from it (`dal-<id>`), so uniqueness follows from `id`
uniqueness — this migration's index is the DB-level enforcement of that.

The index is global, not tenant-scoped: containerlab's runtime namespace is
host-global, not tenant-scoped, so a `(tenant_id, instance_name)` unique
constraint would not be sufficient to prevent a runtime name collision across
tenants.

Existing rows' `instance_name` values are NEVER rewritten by this migration —
only new rows going forward use the new naming scheme.

Duplicate-checking and the index build happen in one ordinary transaction
(no `CREATE INDEX CONCURRENTLY`, no `autocommit_block()`), unlike migration
`0056`'s NOT VALID/VALIDATE split. That split existed there to avoid holding
an `AccessExclusiveLock` across a long validation scan; here, duplicate
detection and index creation must instead be atomic *with each other* to
avoid a TOCTOU race where a row could be inserted with a name that collides
with another row between the duplicate check and the index build. This table
is also young enough that a normal blocking index build is an acceptable
tradeoff. Keeping both steps in the migration's single ambient transaction
also means a failure at any point rolls back atomically — there is no
durable partial-state risk here the way there was in 0056, so (unlike 0056)
no existence guard is needed around the index creation.

Revision ID: 0057_lab_instance_name_unique
Revises: 0056_lab_instance_worker
"""

from __future__ import annotations

from alembic import op

revision = "0057_lab_instance_name_unique"
down_revision = "0056_lab_instance_worker"
branch_labels = None
depends_on = None

TABLE = "lab_instances"
INDEX = "uq_lab_instances_instance_name"


def upgrade() -> None:
    # `SHARE MODE` blocks concurrent writers (so no new duplicate can be
    # inserted underneath this check) while still permitting concurrent
    # reads, for the remainder of this transaction — i.e. until the unique
    # index below has actually been built.
    op.execute(f"LOCK TABLE {TABLE} IN SHARE MODE;")
    # Expressed as a single `DO $$ ... RAISE EXCEPTION ...` block (rather than
    # a Python-side query + conditional `raise`) so this migration stays
    # renderable as pure SQL via `alembic upgrade --sql`, and so the actual
    # duplicate names and counts are included in the database's own error
    # message rather than requiring a live connection to report them.
    # `TABLE` is this module's fixed, hardcoded constant, never external
    # input — ruff's SQL-injection heuristic (S608) still fires on the
    # embedded `SELECT`, so it's suppressed explicitly on that line below
    # rather than broadening `alembic/versions/*`'s per-file ignores.
    duplicate_check_sql = f"""
        DO $$
        DECLARE
            dupes text;
        BEGIN
            SELECT string_agg(format('%s (x%s)', instance_name, cnt), ', ')
            INTO dupes
            FROM (
                SELECT instance_name, COUNT(*) AS cnt
                FROM {TABLE}
                GROUP BY instance_name
                HAVING COUNT(*) > 1
            ) AS d;
            IF dupes IS NOT NULL THEN
                RAISE EXCEPTION
                    'cannot add a unique index on %.instance_name: duplicate '
                    'values present: %', '{TABLE}', dupes;
            END IF;
        END $$;
        """  # noqa: S608
    op.execute(duplicate_check_sql)
    op.create_index(INDEX, TABLE, ["instance_name"], unique=True)


def downgrade() -> None:
    op.drop_index(INDEX, table_name=TABLE)
