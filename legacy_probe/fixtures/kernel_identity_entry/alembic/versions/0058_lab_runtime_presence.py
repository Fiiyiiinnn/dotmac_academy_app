"""Add a worker-owned `lab_instances.runtime_presence` column.

`_capacity_available` (`app/services/lab_operations.py`) used to derive
capacity entirely from `LabInstance.status IN ("provisioning", "active",
"resetting")` — a field that ALSO carries lifecycle/UI meaning and cannot
simultaneously and correctly express "operation failed, runtime presence
uncertain" (previously overloaded onto `status="active"` conservatively) vs
"escalated for human review, but runtime might still exist" (previously
`status="error"`, NOT capacity-counted, even when it should be).

This migration adds `runtime_presence`, a worker-owned column independent of
`status`, restricted by CHECK to exactly three values: `absent` (proven
absent), `present` (observed or successfully created), `unknown` (external
mutation began, result uncertain). Application code (`app/services/
lab_operations.py`, `app/services/lab_lifecycle.py`, `app/services/
lab_jobs.py`) is responsible for the transitions; this migration only adds
the column, its constraint, and a conservative backfill.

Backfill is deliberately conservative, not accurate: `status == "reaped"`
rows become `absent` (already destroyed); every other existing status
becomes `unknown` (provenance is ambiguous for pre-cutover rows — treating
them as `unknown` keeps them capacity-counted, identical to today's
behavior for an active row, rather than silently under-counting). No
special one-time reconciliation pass is required or wanted: a backfilled
`unknown` row that never goes through another worker action may stay
`unknown` indefinitely — this is safe, not a bug — and rows converge
naturally as they cycle through real deploy/destroy/reconcile actions.

`ADD COLUMN ... server_default` in Postgres populates every existing row
with that default at add time — so the column is added with
`server_default='absent'` (populating every row `absent` first), and then a
single explicit `UPDATE` repaints every non-reaped row to `unknown`.

Lock duration (the same lesson 0056's own module docstring already
documents for `ck_lab_operations_kind`/`ck_lab_operations_state`): `ADD
COLUMN ... DEFAULT` takes `AccessExclusiveLock`, but Postgres 11+'s
"fast default" makes that ADD itself metadata-only, so the lock is held only
briefly — UNLESS something else shares its transaction, in which case the
lock is held for as long as that whole transaction runs. The backfill
`UPDATE` below touches every non-reaped row on a table Codex review has
flagged (more than once) as technically unbounded in row count — running it
in the SAME transaction as the ADD would extend the ADD's AccessExclusiveLock
across the UPDATE's full duration too, blocking all concurrent reads/writes
on `lab_instances` for that whole time, even though the UPDATE itself only
actually needs an ordinary `RowExclusiveLock`. `upgrade()` therefore splits
the ADD COLUMN, the backfill UPDATE, and the CHECK constraint's NOT
VALID/VALIDATE halves into their own `op.get_context().autocommit_block()`s
— exactly 0056's pattern — so each commits independently and no single
transaction holds a table-wide exclusive lock across the backfill scan. The
CHECK constraint follows 0056's NOT VALID + VALIDATE split for the identical
reason: `VALIDATE CONSTRAINT` scans every existing row, so it must not share
a transaction with the ADD COLUMN either. Each `autocommit_block()` durably
commits before Alembic records this revision as applied, so — again exactly
like 0056 — the ADD COLUMN and the ADD CONSTRAINT ... NOT VALID statements
are guarded by an existence check (`information_schema.columns` /
`pg_constraint`) so a rerun after a partial failure is a no-op instead of a
hard failure on "column/constraint already exists". The backfill `UPDATE` is
naturally idempotent — repainting already-`'unknown'` rows to `'unknown'`
again is harmless — so it does not need a guard of its own, unlike the two
ADD statements around it.

Rollout and rollback concerns: unlike a typical schema-only migration, THIS
migration must be deployed with a synchronized lab-worker restart — do not
let old (pre-`runtime_presence`) worker code keep running against the new
schema, even briefly. An old worker's code has no notion of
`runtime_presence` at all: it can still successfully deploy a genuinely
running lab, but it never sets the column on that row, which then stays at
the schema default (`'absent'`) forever. Neither of `reconcile_runtime`'s
repair branches in `app/services/lab_jobs.py` would ever catch this
afterward: `db_only_repairs` excludes every "active"-status row outright,
and `missing_runtime` only fires when the runtime is NOT found in
inventory — but here the runtime genuinely IS present (the old worker really
did deploy it), so neither branch's mismatch-detection logic ever triggers.
The row would silently and permanently escape capacity accounting (see
`_capacity_available` in `app/services/lab_operations.py`), letting the
configured concurrency cap be bypassed indefinitely by exactly the instances
this whole design exists to protect. This is a rollout-ordering requirement,
not something this migration (or a version-compatibility check in
application code, which would be disproportionate new machinery for this
slice) can enforce on its own.

Grants: `academy_lab_worker` already holds a table-wide `GRANT SELECT,
UPDATE ON lab_instances` from migration 0056, and `platform_api` already
holds table-wide `SELECT` — both privileges already extend automatically to
this new column with no further statement needed, since PostgreSQL table-
level grants are not scoped to the column set that existed at grant time.
`app_user`'s INSERT grant on this table, however, IS column-scoped (see
0056), so a new column is excluded from it by construction unless
explicitly added — this migration must NOT add `runtime_presence` to that
list. To make this ACL posture explicit and durably recorded at this
revision (mirroring 0056's own explicit-grant pattern) rather than relying
on an inherited, undocumented side effect of table-level grants, this
migration re-issues the exact same `lab_instances` grant statements 0056
established: `REVOKE ALL ... FROM app_user, platform_api` followed by the
same `SELECT`/column-scoped-`INSERT`/`SELECT, UPDATE` grants. `app_admin`'s
migration/offline privileges are untouched (never revoked by 0056 either).

Revision ID: 0058_lab_runtime_presence
Revises: 0057_lab_instance_name_unique
"""

from __future__ import annotations

from alembic import op

revision = "0058_lab_runtime_presence"
down_revision = "0057_lab_instance_name_unique"
branch_labels = None
depends_on = None

TABLE = "lab_instances"
CHECK_CONSTRAINT = "ck_lab_instances_runtime_presence"
# Exactly 0056's app_user INSERT column list for this table — runtime_presence
# is deliberately NOT included; it is worker-owned like status/consoles/error.
APP_USER_INSERT_COLUMNS = "id, tenant_id, activity_id, person_id, instance_name, seed"


def upgrade() -> None:
    # Guarded (existence-checked) and in its own autocommit_block(): this
    # ADD is fast/metadata-only (Postgres 11+ "fast default"), but only if
    # nothing else shares its transaction — committing immediately here
    # releases the AccessExclusiveLock before the backfill UPDATE below runs.
    # See the module docstring's "Lock duration" section for the full
    # reasoning and 0056's precedent for this exact guard/autocommit shape.
    with op.get_context().autocommit_block():
        op.execute(
            "DO $$ BEGIN "
            "IF NOT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'lab_instances' "
            "AND column_name = 'runtime_presence') THEN "
            "ALTER TABLE lab_instances ADD COLUMN runtime_presence VARCHAR(16) "
            "NOT NULL DEFAULT 'absent'; "
            "END IF; END $$;"
        )
    # Own autocommit_block, now that the ADD's exclusive lock has already
    # been released: this UPDATE only needs an ordinary RowExclusiveLock for
    # its duration. Naturally idempotent (repainting an already-'unknown' row
    # to 'unknown' again is harmless), so no existence guard is needed here.
    with op.get_context().autocommit_block():
        op.execute(f"UPDATE {TABLE} SET runtime_presence = 'unknown' WHERE status != 'reaped';")  # noqa: S608
    # NOT VALID + VALIDATE split, mirroring 0056's ck_lab_operations_kind/
    # ck_lab_operations_state exactly: VALIDATE scans every existing row, so
    # it must not share a transaction with the ADD COLUMN above either.
    add_check_constraint_sql = (
        "DO $$ BEGIN "  # noqa: S608
        "IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = "
        f"'{CHECK_CONSTRAINT}' AND conrelid = '{TABLE}'::regclass) THEN "
        f"ALTER TABLE {TABLE} ADD CONSTRAINT {CHECK_CONSTRAINT} "
        "CHECK (runtime_presence IN ('absent', 'present', 'unknown')) NOT VALID; "
        "END IF; END $$;"
    )
    with op.get_context().autocommit_block():
        op.execute(add_check_constraint_sql)
    with op.get_context().autocommit_block():
        op.execute(f"ALTER TABLE {TABLE} VALIDATE CONSTRAINT {CHECK_CONSTRAINT};")

    # Re-establish the exact grant posture 0056 established for this table —
    # see module docstring for why this is a no-op in effect but made
    # explicit and durably recorded at this revision. Metadata-only and
    # cheap, so these run in the final ambient transaction like 0056's own
    # grant statements do.
    op.execute(f"REVOKE ALL PRIVILEGES ON {TABLE} FROM app_user, platform_api;")
    op.execute(f"GRANT SELECT ON {TABLE} TO app_user, platform_api;")
    op.execute(f"GRANT INSERT ({APP_USER_INSERT_COLUMNS}) ON {TABLE} TO app_user;")
    op.execute(f"GRANT SELECT, UPDATE ON {TABLE} TO academy_lab_worker;")


def downgrade() -> None:
    op.drop_constraint(CHECK_CONSTRAINT, TABLE, type_="check")
    op.execute(f"ALTER TABLE {TABLE} DROP COLUMN runtime_presence;")
    # No grant changes here: dropping the column removes any privilege
    # specific to it, and the table-level grants below are byte-for-byte the
    # same statements 0056 already established at 0057 — restating them
    # keeps this migration's downgrade self-contained without depending on
    # 0056's downgrade never having run, but changes nothing that wasn't
    # already true at 0057.
    op.execute(f"REVOKE ALL PRIVILEGES ON {TABLE} FROM app_user, platform_api;")
    op.execute(f"GRANT SELECT ON {TABLE} TO app_user, platform_api;")
    op.execute(f"GRANT INSERT ({APP_USER_INSERT_COLUMNS}) ON {TABLE} TO app_user;")
    op.execute(f"GRANT SELECT, UPDATE ON {TABLE} TO academy_lab_worker;")
