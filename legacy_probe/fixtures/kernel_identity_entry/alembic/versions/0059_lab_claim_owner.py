"""Add `lab_operations.claimed_host`/`claimed_epoch` — structural claim
ownership for safe worker-restart reclaim.

`claimed_by` (migration 0055) already fences every heartbeat/settlement
comparison and stays the sole fencing value — this migration changes nothing
about that. Its composite string (`f"{hostname}:{pid}:{boot_token}"`,
`app.services.lab_operations.worker_identity()`) is, however, opaque to a
freshly-started worker process that wants to answer one narrow question
without parsing it: "did I myself already claim this exact row, in a
*previous* incarnation on *this* host, that never settled it?" That question
only matters at worker startup, before the ordinary polling loop begins.

`claimed_host` (the worker's hostname) and `claimed_epoch` (`f"{pid}:
{boot_token}"`) are a structural decomposition of the same identity, written
alongside `claimed_by` on every claim, purely so a startup reclaim scan
(`app.services.lab_operations.reclaim_previous_epoch`) can select
"`claimed_host` == my host AND `claimed_epoch` != my epoch" directly, instead
of string-parsing `claimed_by`. Neither column is ever compared in a fencing
predicate (`claim_next`/`heartbeat`/`_refresh_claim`/`_settle`/
`run_claimed`'s staleness checks) — only `claimed_by` is, unchanged.

No backfill, no data migration, no CHECK constraint, no new index: both
columns are nullable with no DDL default, and the existing
`ix_lab_operations_claim` (`state`, `not_before`, `requested_at`) index is
already sufficient for the ordinary claim-queue scan; the new startup reclaim
scan runs once per worker process lifetime, not per poll, so it does not need
its own index. `claimed_host`/`claimed_epoch` are excluded from `app_user`'s
column-scoped INSERT grant (migration 0055's five-column list is restated
here unchanged) for the same reason every other worker-owned column already
is: only `academy_lab_worker` may ever set structural claim ownership.

Grants: this migration re-issues `lab_operations`' full grant posture
explicitly (mirroring 0056/0058's restatement style for `lab_instances`)
rather than relying on the inherited, undocumented side effect of 0055's
table-level grants silently extending to these two new columns. `app_user`
gets table `SELECT` plus the same five-column INSERT grant 0055 established
(unchanged — `claimed_host`/`claimed_epoch` are deliberately NOT added to
it); `platform_api` keeps `SELECT`; `academy_lab_worker` keeps `SELECT,
INSERT, UPDATE`; `app_admin`'s explicit migration/offline grant (established
by 0055 for CI parity with a superuser-owned table) is restated identically.

Rollout and rollback concerns: unlike 0058's `runtime_presence` (which
required a synchronized worker restart), this migration is NOT a hard
same-instant rollout requirement. An old (pre-0059) worker process running
against this new schema simply never sets `claimed_host`/`claimed_epoch` on
the claims it takes — those rows carry `NULL` structural ownership, so the
new startup reclaim scan (`claimed_host IS ...`, `claimed_epoch IS NOT NULL`)
never matches them and leaves them alone by construction. Such a claim falls
back to ordinary lease-expiry recovery (`reconcile_stuck`) exactly as it
would have before this migration existed — safe, just without restart-reclaim's
faster recovery for that one claim. A mixed old-worker/new-schema window is
therefore tolerable, not merely "safe if you're careful" — worth stating
explicitly since it is the exception rather than the rule among this table's
recent migrations.

The reverse direction is NOT equally tolerant, and is a hard ordering
requirement rather than a tolerable window: a 0059-aware worker process
unconditionally reads and writes `claimed_host`/`claimed_epoch` on every
`claim_next` call for as long as it keeps running. If this migration is
downgraded (dropping both columns) while a 0059-aware worker process is
still alive, that worker's code has no way to learn the columns disappeared
mid-lifetime — its very next claim attempt fails with a SQL error (column
does not exist), rather than degrading gracefully the way the forward
direction does. Application code must therefore be stopped or rolled back to
a pre-0059 version BEFORE this migration is downgraded, never after or
concurrently with it — the inverse of the forward case's tolerance, not an
extension of it.

Revision ID: 0059_lab_claim_owner
Revises: 0058_lab_runtime_presence
"""

from __future__ import annotations

from alembic import op

revision = "0059_lab_claim_owner"
down_revision = "0058_lab_runtime_presence"
branch_labels = None
depends_on = None

TABLE = "lab_operations"
# Exactly 0055's app_user INSERT column list — claimed_host/claimed_epoch are
# deliberately NOT included; they are worker-owned like claimed_by/claimed_at.
APP_USER_INSERT_COLUMNS = "id, tenant_id, instance_id, kind, requested_by"


def upgrade() -> None:
    op.execute(
        f"ALTER TABLE {TABLE} ADD COLUMN claimed_host VARCHAR(255);"
    )
    op.execute(
        f"ALTER TABLE {TABLE} ADD COLUMN claimed_epoch VARCHAR(64);"
    )

    # Re-establish the exact grant posture 0055 established for this table —
    # see module docstring for why this is a no-op in effect but made
    # explicit and durably recorded at this revision, mirroring 0056/0058's
    # restatement style for lab_instances.
    op.execute(f"REVOKE ALL PRIVILEGES ON {TABLE} FROM app_user, platform_api;")
    op.execute(f"GRANT SELECT ON {TABLE} TO app_user;")
    op.execute(f"GRANT INSERT ({APP_USER_INSERT_COLUMNS}) ON {TABLE} TO app_user;")
    op.execute(f"GRANT SELECT ON {TABLE} TO platform_api;")
    # Explicit even though app_admin is the table owner in production — CI
    # migrates as the `postgres` superuser, which owns the table there
    # instead (see 0055's own module docstring for the full explanation).
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {TABLE} TO app_admin;")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON {TABLE} TO academy_lab_worker;")


def downgrade() -> None:
    op.execute(f"ALTER TABLE {TABLE} DROP COLUMN claimed_epoch;")
    op.execute(f"ALTER TABLE {TABLE} DROP COLUMN claimed_host;")

    # No grant changes are actually needed (dropping a column removes any
    # privilege specific to it, and nothing here narrows an existing grant),
    # but restate the same posture 0055 established anyway — self-contained
    # and explicit, matching 0058's downgrade precedent for this same reason.
    op.execute(f"REVOKE ALL PRIVILEGES ON {TABLE} FROM app_user, platform_api;")
    op.execute(f"GRANT SELECT ON {TABLE} TO app_user;")
    op.execute(f"GRANT INSERT ({APP_USER_INSERT_COLUMNS}) ON {TABLE} TO app_user;")
    op.execute(f"GRANT SELECT ON {TABLE} TO platform_api;")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {TABLE} TO app_admin;")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON {TABLE} TO academy_lab_worker;")
