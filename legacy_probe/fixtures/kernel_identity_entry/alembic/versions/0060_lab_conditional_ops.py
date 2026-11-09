"""Add worker-owned `lab_operations.origin`/`runtime_precondition` — reconciler
conditional operations (item 4 of 4, restart-reclaim/capacity/reconciler
design).

This is the mechanism that closes the ordering bug an earlier, unauthorized
attempt at this slice got wrong: a "repair" operation the reconciler enqueues
(`db_only_repairs`/`missing_runtime` in `app/services/lab_jobs.py`) used to be
paired with the reconciler ITSELF immediately projecting the repaired
lifecycle state onto `LabInstance` (`status`/`error`/`runtime_presence`),
before the worker ever re-verified anything. If the runtime had already
changed again by the time the worker actually claimed and ran that operation,
the worker had no way to know its precondition no longer held — it would
either duplicate work harmlessly or, worse, tear down consoles / mutate the
runtime based on a stale assumption. `origin` and `runtime_precondition`
let the reconciler express its INTENT ("deploy this only if the runtime is
genuinely absent"; "destroy this only if the runtime is genuinely present")
without asserting the outcome — the worker (`app/services/lab_operations.py`,
`app/services/lab_lifecycle.py`) is the only party that ever observes the
runtime under the host lock and decides whether the precondition still holds,
and it does so BEFORE any console teardown or containerlab mutation, never
after.

`origin` is audit/provenance only — it identifies WHY a conditional operation
was created (`"runtime_repair"` for a conditional deploy, `"runtime_cleanup"`
for a conditional destroy). It never INDEPENDENTLY determines whether a
precondition holds — only `runtime_precondition` (`"absent"`/`"present"`)
does that. `origin` (together with `kind`) IS compared, but only to confirm
which of the two valid conditional shapes a row claims to be
(`app/services/lab_operations.py`'s `is_conditional_deploy`/
`is_conditional_destroy`); a malformed or mismatched shape (any tuple outside
the three this migration's CHECK constraint permits) is already rejected at
the database layer before this application code ever runs, so that
comparison can never itself misclassify an ordinary operation as
conditional or vice versa. Both columns are nullable and NULL for the
overwhelming majority of rows
(every ordinary, unconditional operation `request_idle_reaps`/
`reconcile_stuck`/`reclaim_previous_epoch` create) — this migration adds no
backfill, since every existing row already satisfies the "both NULL" CHECK
branch trivially.

The CHECK constraint (`ck_lab_operations_conditional_runtime`) permits exactly
three shapes: both NULL (ordinary), or one of the two valid
`(kind, origin, runtime_precondition)` triples described above. Explicit
`IS NOT NULL` guards are used on every branch (rather than relying on `IN`/
`=` alone) specifically so Postgres's three-valued CHECK logic cannot
silently accept a half-null combination — a CHECK constraint is satisfied
whenever it does not evaluate to FALSE, and an ordinary `=` comparison against
one NULL column evaluates to NULL (not FALSE), which would let a
half-populated row silently pass without an explicit `IS NOT NULL` on both
sides of every branch.

No NOT VALID/VALIDATE split (unlike 0058's `ck_lab_instances_runtime_presence`,
which needed one): that split exists specifically because 0058 added a NOT
NULL column with a backfill UPDATE touching every existing row, and
`VALIDATE CONSTRAINT` would otherwise share a lock-holding transaction with
that backfill scan. Neither new column here has a backfill: both start NULL
for every existing row (no `ALTER TABLE ... ADD COLUMN ... DEFAULT` at all),
which trivially satisfies the "both NULL" CHECK branch for every row that
already exists — there is no existing-row scan whose cost or lock duration
this migration needs to defer. A plain `ADD CONSTRAINT ... CHECK (...)` (no
`NOT VALID`) still validates only against the current (all-NULL-in-these-
columns) row set, which is cheap, so it is added directly.

Grants: mirrors 0055/0056/0058/0059's exact restatement style for
`lab_operations`. `app_user`'s column-scoped INSERT grant remains BYTE-
IDENTICAL to 0055's five-column list (`id, tenant_id, instance_id, kind,
requested_by`) — `origin`/`runtime_precondition` are deliberately NOT added
to it, since only the worker (via the reconciler's own `academy_lab_worker`-
authenticated session) may ever set them, exactly like every other worker-
owned column on this table. `platform_api` keeps `SELECT`; `academy_lab_worker`
keeps `SELECT, INSERT, UPDATE`; `app_admin`'s explicit migration/offline grant
(established by 0055, restated by 0059) is restated identically here too.

Rollout and rollback concerns: forward rollout is a HARD synchronization
requirement, NOT a tolerable mixed-version window — the inverse of 0059's own
precedent for `claimed_host`/`claimed_epoch`, and more severe than 0058's.
An old (pre-0060) worker process has no concept of `origin`/
`runtime_precondition` at all: if a NEW (post-0060) reconciler enqueues a
conditional row while an old worker is still running, that old worker's
`run_claimed()` has no `is_conditional_deploy`/`is_conditional_destroy`
branch to take — it claims the row via its ordinary, UNCONDITIONAL deploy/
destroy code path instead, which can tear down or redeploy a runtime the
conditional operation's whole precondition-check design exists specifically
to protect. This is unlike 0059's `claimed_host`/`claimed_epoch` (an old
worker simply leaves two audit columns NULL and degrades gracefully to
lease-expiry recovery) and unlike 0058's `runtime_presence` (an old worker
under-counts capacity, a safety-preserving direction): here, an old worker
does not degrade gracefully or under-count — it takes an actively wrong,
potentially destructive action against a live runtime. Both the
`academy-labs` worker AND the `lab-reconcile` reconciler process MUST be
running post-0060 code before EITHER is allowed to enqueue or claim a row
against this schema; this is a mandatory input to the (separate,
not-yet-authorized) production rollout runbook, not something this migration
or any new application code can enforce or retrofit onto an
already-running old worker process by itself.

Downgrade must NEVER silently discard live conditional intent for an old
worker to misinterpret as unconditional: dropping `runtime_precondition` out
from under a still-`queued` or still-`claimed` conditional row would leave an
old (pre-0060) worker to produce a plain, unconditional deploy/destroy against
it — exactly the ordering bug this whole design exists to close, just moved
to the schema boundary instead of the code boundary. So `downgrade()` first
sets a bounded `lock_timeout` (so a concurrent, ordinary claim transaction
cannot hang this downgrade indefinitely), takes an `ACCESS EXCLUSIVE` lock on
`lab_operations`, and explicitly checks for any row with
`state IN ('queued', 'claimed')` AND `runtime_precondition IS NOT NULL`. If
any such row exists, the downgrade raises and leaves the schema/constraint/
columns untouched — an operator must first let those rows settle (or fail
them out) before downgrading is safe. Only when none exist does the downgrade
proceed to drop the CHECK constraint and both columns, then restate the
grant posture. `downgrade()` resets its `lock_timeout` back to `DEFAULT`
before returning (this repo's `alembic/env.py` runs an entire multi-revision
upgrade/downgrade inside one shared transaction, so a `SET LOCAL` left in
place would otherwise silently leak into every later migration step in the
same invocation); it also refuses up front under `alembic downgrade --sql`
(offline mode), since its refusal check must read live row state under a
real connection. `upgrade()` takes the same bounded, reset `lock_timeout`
for consistency and to avoid an unbounded wait against a busy table during
rollout.

Revision ID: 0060_lab_conditional_ops
Revises: 0059_lab_claim_owner
"""

from __future__ import annotations

from sqlalchemy import text

from alembic import context, op

revision = "0060_lab_conditional_ops"
down_revision = "0059_lab_claim_owner"
branch_labels = None
depends_on = None

TABLE = "lab_operations"
CHECK_CONSTRAINT = "ck_lab_operations_conditional_runtime"
# Exactly 0055's app_user INSERT column list for this table — origin/
# runtime_precondition are deliberately NOT included; they are worker-owned
# like claimed_by/claimed_host/claimed_epoch.
APP_USER_INSERT_COLUMNS = "id, tenant_id, instance_id, kind, requested_by"

_CHECK_SQL = (
    f"ALTER TABLE {TABLE} ADD CONSTRAINT {CHECK_CONSTRAINT} CHECK ("
    "(origin IS NULL AND runtime_precondition IS NULL) "
    "OR (kind = 'deploy' AND origin IS NOT NULL AND origin = 'runtime_repair' "
    "AND runtime_precondition IS NOT NULL AND runtime_precondition = 'absent') "
    "OR (kind = 'destroy' AND origin IS NOT NULL AND origin = 'runtime_cleanup' "
    "AND runtime_precondition IS NOT NULL AND runtime_precondition = 'present')"
    ");"
)


def _restate_grants() -> None:
    op.execute(f"REVOKE ALL PRIVILEGES ON {TABLE} FROM app_user, platform_api;")
    op.execute(f"GRANT SELECT ON {TABLE} TO app_user;")
    op.execute(f"GRANT INSERT ({APP_USER_INSERT_COLUMNS}) ON {TABLE} TO app_user;")
    op.execute(f"GRANT SELECT ON {TABLE} TO platform_api;")
    # Explicit even though app_admin is the table owner in production — CI
    # migrates as the `postgres` superuser, which owns the table there
    # instead (see 0055's own module docstring for the full explanation).
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {TABLE} TO app_admin;")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON {TABLE} TO academy_lab_worker;")


def upgrade() -> None:
    # Bounded so a concurrent, ordinary claim/settle transaction cannot
    # hang this upgrade indefinitely — mirrors downgrade()'s own
    # reasoning; reset afterward so it does not leak into whatever
    # migration runs next in the same alembic invocation.
    op.execute("SET LOCAL lock_timeout = '5s';")
    op.execute(f"ALTER TABLE {TABLE} ADD COLUMN origin VARCHAR(32);")
    op.execute(f"ALTER TABLE {TABLE} ADD COLUMN runtime_precondition VARCHAR(16);")
    op.execute(_CHECK_SQL)
    op.execute("SET LOCAL lock_timeout = DEFAULT;")
    _restate_grants()


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError(
            f"refusing to generate offline SQL for {revision}'s downgrade: it "
            "must read live row state under a real database connection to "
            "decide whether any queued/claimed conditional operation exists; "
            "run this downgrade online instead."
        )
    # Bounded so a concurrent, ordinary claim/settle transaction contending
    # for this same row-set cannot hang this downgrade indefinitely — mirrors
    # reclaim_previous_epoch's own `SET LOCAL lock_timeout` reasoning in
    # app/services/lab_operations.py.
    op.execute("SET LOCAL lock_timeout = '5s';")
    op.execute(f"LOCK TABLE {TABLE} IN ACCESS EXCLUSIVE MODE;")
    # `runtime_precondition IS NOT NULL` alone is sufficient once the CHECK
    # constraint (fixed to guard both columns with explicit `IS NOT NULL` on
    # every conditional branch — see the module docstring) is in place: the
    # CHECK is the sole enforcement point and is not privilege-scoped, so it
    # applies to every writer including `app_admin`, making a half-null
    # (origin set, runtime_precondition NULL) row impossible to persist in
    # the first place. `OR origin IS NOT NULL` is added anyway, defensively,
    # so this refusal check does not silently depend on the CHECK constraint
    # never regressing — checking both columns costs nothing here and keeps
    # the invariant self-evident from this query alone.
    live_conditional = op.get_bind().execute(
        text(
            f"SELECT COUNT(*) FROM {TABLE} "  # noqa: S608
            "WHERE state IN ('queued', 'claimed') "
            "AND (runtime_precondition IS NOT NULL OR origin IS NOT NULL)"
        )
    ).scalar()
    if live_conditional:
        raise RuntimeError(
            f"refusing to downgrade {revision}: {live_conditional} row(s) in "
            "lab_operations are queued/claimed with a non-null "
            "runtime_precondition — an old (pre-0060) worker would "
            "misinterpret them as unconditional deploy/destroy operations "
            "and could tear down or redeploy a runtime based on a stale "
            "assumption. Let these operations settle (or fail them out) "
            "before downgrading."
        )
    op.drop_constraint(CHECK_CONSTRAINT, TABLE, type_="check")
    op.execute(f"ALTER TABLE {TABLE} DROP COLUMN runtime_precondition;")
    op.execute(f"ALTER TABLE {TABLE} DROP COLUMN origin;")
    op.execute("SET LOCAL lock_timeout = DEFAULT;")
    _restate_grants()
