"""Cross-tenant lab operation worker and enqueue-only idle reaper.

Unlike the per-request lifecycle helpers in :mod:`app.services.lab_lifecycle`
(which take ``db`` and only ``flush`` — the request handler owns the
transaction), these are background jobs that run across ALL tenants. They need
an offline/BYPASSRLS session that can see every tenant's rows and they OWN their
transaction boundary, so they ``commit``. Containerlab execution is narrower:
it requires the exact dedicated ``academy_lab_worker`` identity.

Use :func:`lab_worker_session` for containerlab worker/reconciler execution.
The pre-existing :func:`admin_session` remains the generic offline session used
by unrelated scheduled jobs and metrics; keeping the two credentials separate
prevents the web host from needing the lab worker DSN.

These two named offline credential boundaries use Kernel Session construction
with their dedicated engines. They do not bind the online runtime or enter its
tenant/platform boundaries; callers retain explicit transaction ownership.

* :func:`drain_once` — claim and execute durable ``lab_operations`` rows.
* :func:`request_idle_reaps` — enqueue destroy intents without touching the
  engine (the web-host timer calls this).
* :func:`recover_missing_consoles` — repair worker-restart ttyd loss before
  draining more operations, without redeploying a live lab.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from dotmac_kernel.session_runtime import DatabaseRuntime
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

import app.models.lab  # noqa: F401  # register product tables for CLI workers
from app.config import LabExecutionPolicy, settings
from app.models.lab import LabConsoleActivity, LabInstance, LabOperation
from app.services import lab_operations
from app.services.host_lock import HostLockUnavailable
from app.services.lab_lifecycle import (
    console_pids,
    console_processes,
    kill_consoles,
    start_console,
)
from app.services.labengine.interface import LabEngine


@contextmanager
def admin_session() -> Iterator[Session]:
    """Yield the repository's generic cross-tenant offline Session.

    This preserves the established session used by metrics and non-lab timers.
    Containerlab ownership paths must use :func:`lab_worker_session` instead.
    """
    engine = create_engine(settings.migration_database_url, future=True)
    try:
        runtime = DatabaseRuntime(engine=engine)
        db = runtime.session_factory()
        try:
            yield db
        finally:
            db.close()
    finally:
        engine.dispose()


@contextmanager
def lab_worker_session() -> Iterator[Session]:
    """Yield a dedicated, live-verified ``academy_lab_worker`` Session."""
    worker_url = settings.lab_worker_database_url
    try:
        worker_role = make_url(worker_url).username
    except Exception as exc:
        raise RuntimeError("LAB_WORKER_DATABASE_URL is invalid") from exc
    if worker_role != "academy_lab_worker":
        raise RuntimeError("LAB_WORKER_DATABASE_URL must authenticate as academy_lab_worker")
    engine = create_engine(worker_url, future=True)
    try:
        runtime = DatabaseRuntime(engine=engine)
        db = runtime.session_factory()
        try:
            if db.scalar(text("SELECT current_user")) != "academy_lab_worker":
                raise RuntimeError("lab worker database session is not academy_lab_worker")
            # ``lab_operations`` has FORCE ROW LEVEL SECURITY with a tenant_id GUC
            # policy (see 0055_lab_operations.py). Being the worker role is not
            # enough on its own — if BYPASSRLS were ever missing or revoked from
            # an already-existing role, the worker would silently see an empty
            # queue (RLS resolves against a NULL tenant GUC) and stop processing
            # with no error at all. Check the actual role attribute, not just the
            # role name, so that failure mode raises instead of going quiet.
            if not db.scalar(text("SELECT rolbypassrls FROM pg_roles WHERE rolname = 'academy_lab_worker'")):
                raise RuntimeError("academy_lab_worker role does not have BYPASSRLS")
            safe_posture = db.scalar(
                text(
                    """SELECT NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole
                    AND NOT rolreplication AND NOT rolinherit
                    AND NOT EXISTS (
                        SELECT 1 FROM pg_auth_members m
                        WHERE m.member = r.oid OR m.roleid = r.oid
                    )
                FROM pg_roles r
                WHERE r.rolname = 'academy_lab_worker'"""
                )
            )
            if not safe_posture:
                raise RuntimeError("academy_lab_worker role has unsafe attributes or role memberships")
            owns = db.scalar(
                text(
                    """SELECT EXISTS (
                    SELECT 1 FROM pg_database
                    WHERE datname = current_database()
                      AND pg_get_userbyid(datdba) = 'academy_lab_worker'
                ) OR EXISTS (
                    SELECT 1 FROM pg_namespace
                    WHERE pg_get_userbyid(nspowner) = 'academy_lab_worker'
                      AND nspname NOT IN ('pg_catalog', 'information_schema')
                      AND nspname NOT LIKE 'pg_toast%'
                ) OR EXISTS (
                    SELECT 1
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE pg_get_userbyid(c.relowner) = 'academy_lab_worker'
                      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
                      AND n.nspname NOT LIKE 'pg_toast%'
                      AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
                )"""
                )
            )
            if owns:
                raise RuntimeError("academy_lab_worker must not own the database, schema, or application relations")
            yield db
        finally:
            db.close()
    finally:
        engine.dispose()


def drain_once(
    db: Session,
    engine: LabEngine,
    *,
    policy: LabExecutionPolicy,
    claimed_by: str | None = None,
    claimed_host: str | None = None,
    claimed_epoch: str | None = None,
) -> int:
    """Claim and execute ready operations until the queue has no ready row.

    Each claim is committed before external work starts, keeping
    ``FOR UPDATE SKIP LOCKED`` inside a short transaction. Settlement is fenced
    by the worker identity in :func:`lab_operations.run_claimed`.

    When ``claimed_by`` is left as its default, this process computes its own
    ``WorkerIdentity`` (host/epoch included) fresh here. A caller that already
    computed its own ``WorkerIdentity`` (the CLI's main polling loop, so the
    exact same identity flows through as the one used for its startup
    restart-reclaim call) should pass all three of ``claimed_by``/
    ``claimed_host``/``claimed_epoch`` explicitly instead of relying on this
    default. Passing only ``claimed_by`` (as this test suite does throughout)
    keeps the prior behavior — no structural ownership fields recorded —
    since an arbitrary test string isn't necessarily a real
    ``WorkerIdentity``-shaped value.
    """
    if engine.execution_policy is not policy:
        raise ValueError("engine must share the worker pass policy")
    if claimed_by is None:
        identity = lab_operations.worker_identity_parts()
        worker = identity.claimed_by
        claimed_host = identity.host
        claimed_epoch = identity.epoch
    else:
        worker = claimed_by
    completed = 0
    while True:
        op = lab_operations.claim_next(
            db,
            policy=policy,
            claimed_by=worker,
            claimed_host=claimed_host,
            claimed_epoch=claimed_epoch,
        )
        if op is None:
            db.rollback()
            break
        operation_id = op.id
        db.commit()
        outcome = lab_operations.run_claimed(
            db,
            operation_id=operation_id,
            policy=policy,
            claimed_by=worker,
            engine=engine,
        )
        if outcome == "succeeded":
            completed += 1
    return completed


def request_idle_reaps(db: Session) -> int:
    """Enqueue destroy intents for idle instances; never call the lab engine."""
    from app.services.settings_store import effective

    cutoff = datetime.now(UTC) - timedelta(minutes=effective(db).lab_idle_minutes)
    # Last activity is the later of the worker-owned lifecycle stamp and the web
    # tier's console heartbeat (a separate observation table; see migration
    # 0063). greatest() ignores NULLs, so an instance with neither stays unreaped
    # exactly as before.
    idle = db.scalars(
        select(LabInstance)
        .outerjoin(LabConsoleActivity, LabConsoleActivity.instance_id == LabInstance.id)
        .where(LabInstance.status == "active")
        .where(func.greatest(LabInstance.last_active_at, LabConsoleActivity.last_seen_at) < cutoff)
    ).all()

    requested = 0
    for inst in idle:
        op = lab_operations.enqueue(db, instance=inst, kind="destroy", requested_by=None)
        if op.kind == "destroy":
            requested += 1
    db.commit()
    return requested


def sweep_orphan_consoles(db: Session) -> int:
    """Kill ttyd consoles whose instance is no longer live; return the count.

    Per-instance teardown is best effort — the process outlives its instance
    whenever ``destroy`` is skipped, the row is deleted, or the killing process
    lacks permission. Nothing then reaps it, so consoles accumulate: this sweep is
    the backstop that makes the leak self-correcting rather than permanent.

    A console is an orphan when its instance id (parsed from the ttyd ``-b`` base
    path) has no ``provisioning``/``resetting``/``active`` row. Cross-tenant, so it needs the
    offline session :func:`admin_session` yields — a tenant-scoped session
    would see another tenant's live console as an orphan and kill it.
    """
    running = console_pids()
    if not running:
        return 0
    # Presence-based, not status-based: an escalated-but-possibly-present
    # runtime (`status="error"`) must still be protected — status alone
    # cannot express physical runtime existence (see app/models/lab.py).
    live = {
        str(row)
        for row in db.scalars(
            select(LabInstance.id).where(LabInstance.runtime_presence.in_(("present", "unknown")))
        ).all()
    }
    return kill_consoles(pid for instance_id, pids in running.items() if instance_id not in live for pid in pids)


def recover_missing_consoles(db: Session, engine: LabEngine, *, policy: LabExecutionPolicy) -> int:
    """Recreate missing Linux-node consoles for live instances.

    The worker is the sole executor of this path.  A fresh, ownership-checked
    inspection must succeed before any ttyd process or projection is changed;
    an open operation, unknown/absent runtime, or inspection failure is skipped.
    """
    if engine.execution_policy is not policy:
        raise ValueError("engine must share the worker pass policy")
    open_operation = (
        select(LabOperation.id)
        .where(LabOperation.instance_id == LabInstance.id)
        .where(LabOperation.state.in_(lab_operations.OPEN_STATES))
        .exists()
    )
    instances = db.scalars(
        select(LabInstance)
        .where(LabInstance.status == "active")
        .where(LabInstance.runtime_presence == "present")
        .where(~open_operation)
    ).all()
    running = console_processes()
    if running is None:
        return 0
    recovered = 0
    for instance in instances:
        recorded = instance.consoles or {}
        existing = running.get(str(instance.id), {})
        recorded_linux = {
            node: spec for node, spec in recorded.items() if not str(spec.get("kind") or "").startswith("vr")
        }
        # A normal successful provision records every node.  Empty projection
        # is therefore suspicious; otherwise inspect only when a Linux node is
        # missing or the recorded port disagrees with the live process table.
        # Healthy and RouterOS-only labs take the cheap no-inspection path.
        needs_recovery = not recorded or any(
            node not in existing
            or not existing[node]
            or (existing[node][0][1] is not None and existing[node][0][1] != spec.get("port"))
            for node, spec in recorded_linux.items()
        )
        if not needs_recovery:
            continue
        try:
            handle = engine.inspect_running(instance.instance_name)
        except (HostLockUnavailable, RuntimeError):
            continue
        if handle is None:
            continue
        consoles: dict = {}
        for node, cname in handle.nodes.items():
            kind = handle.kinds.get(node)
            spec = {"kind": kind, "mgmt": handle.mgmt.get(node)}
            if str(kind or "").startswith("vr"):
                consoles[node] = spec
                continue
            node_processes = existing.get(node, [])
            if node_processes:
                live_port = node_processes[0][1]
                # A matching process with an unparseable port must not cause a
                # duplicate ttyd launch. Preserve the old projection until the
                # next unambiguous observation instead.
                spec["port"] = live_port if live_port is not None else (recorded.get(node) or {}).get("port")
            else:
                port = start_console(cname, f"/labs/instances/{instance.id}/console/{node}")
                spec["port"] = port
                if port is not None:
                    recovered += 1
            consoles[node] = spec
        instance.consoles = consoles
        db.flush()
    return recovered


def reconcile_runtime(db: Session, engine: LabEngine, *, policy: LabExecutionPolicy) -> tuple[int, int]:
    """Cross-check containerlab inventory against rows and repair drift.

    Returns ``(queued_repairs, destroyed_rowless_orphans)``. Only Academy's
    ``dal-`` namespace is eligible for direct cleanup; unrelated containerlab
    runtimes on the same host are never touched.

    Structured in four phases so that host-lock contention (raised as
    :class:`~app.services.host_lock.HostLockUnavailable`, propagated to the
    caller) can never leave a half-applied repair plan behind:

    1. Locked inventory (``engine.inventory()`` — the lock is acquired and
       released inside the engine call itself).
    2. Database reads and repair-plan calculation. No row locks and no host
       lock are held here — this is pure computation over already-fetched
       data.
    3. Locked rowless-orphan destroys (``engine.destroy()`` per orphan, each
       independently acquiring/releasing the host lock). No database row is
       mutated in this phase, so if the host lock is contended partway
       through this loop, there is nothing to unwind.
    4. One more locked, fresh ``engine.inventory()`` call (replacing Phase
       1's snapshot for every decision below), followed by database
       projections/enqueues, only after every host operation this pass
       needed has already completed. Phase 3's destroys each carry their
       own multi-minute timeout, so real time passes between Phase 1/2's
       snapshots and Phase 4's mutations — long enough for an unrelated,
       concurrent operation (e.g. a user-initiated reset running via the
       normal worker path) to change an instance's true state, INCLUDING
       its actual runtime existence, in the meantime. Phase 4 therefore
       re-validates each candidate's CURRENT status, CURRENT open-operation
       membership, AND CURRENT runtime presence immediately before mutating
       it, rather than trusting either the Phase 1 or Phase 2 snapshot for
       anything beyond "this instance was structurally interesting enough
       to look at again" — otherwise a freshly-succeeded redeploy could be
       paved over with a needless destroy+redeploy cycle enqueued against
       stale data. (This function has now had four rounds of staleness
       hardening across its database and runtime state; a further
       staleness angle beyond this should be raised as a tracked decision
       rather than another silent patch here.)

    If either ``engine.inventory()`` call or an ``engine.destroy()`` call
    raises ``HostLockUnavailable``, this function does not catch it: it
    propagates to :func:`app.cli._lab_reconcile`, which rolls back this
    pass's (nonexistent, by construction — phases 1-3 make no database
    writes, and Phase 4's own re-inventory call happens before any of its
    mutations) and exits cleanly rather than raising, so the reconciler
    simply retries on its own 1-minute timer.
    """
    # Phase 1 — locked inventory.
    if engine.execution_policy is not policy:
        raise ValueError("engine must share the worker pass policy")
    runtime = engine.inventory()

    # Phase 2 — database reads and repair-plan calculation; no locks held.
    rows = db.scalars(select(LabInstance)).all()
    if len({row.instance_name for row in rows}) != len(rows):
        raise RuntimeError("duplicate lab instance names prevent safe runtime reconciliation")
    by_name = {row.instance_name: row for row in rows}
    open_instance_ids = set(
        db.scalars(select(LabOperation.instance_id).where(LabOperation.state.in_(lab_operations.OPEN_STATES))).all()
    )

    rowless_orphans = [name for name in sorted(runtime) if name.startswith("dal-") and name not in by_name]
    db_only_repairs = [
        instance
        for name in sorted(runtime)
        if name.startswith("dal-")
        and (instance := by_name.get(name)) is not None
        # "error" is deliberately excluded from eligibility, not just from the
        # live-status tuple below: Stream A's destroy-escalation feature sets
        # status="error" specifically to stop automatic destroy retries once
        # cumulative failures hit its threshold (the idle reaper already
        # respects this by only selecting "active" instances). Without this
        # exclusion, an escalated instance whose runtime is still present
        # would be treated as "non-live" here and get ANOTHER automatic
        # destroy enqueued — bypassing the escalation's intent through a
        # different code path than the one it was designed to block.
        and instance.status not in ("provisioning", "active", "resetting", "error")
        and instance.id not in open_instance_ids
    ]
    missing_runtime = [
        instance
        for instance in rows
        if instance.status in ("provisioning", "active", "resetting")
        and instance.instance_name not in runtime
        and instance.id not in open_instance_ids
    ]

    # Phase 3 — locked rowless-orphan destroys. No database row is mutated
    # above or here, so a HostLockUnavailable raised mid-loop leaves nothing
    # for the caller to roll back.
    destroyed = 0
    for name in rowless_orphans:
        engine.destroy(name)
        destroyed += 1

    # Phase 1's runtime snapshot is just as capable of going stale across
    # Phase 3's (potentially long, per-orphan) destroy loop as Phase 2's
    # database snapshot — a concurrent deploy elsewhere can SETTLE during
    # that window, making an instance no longer "missing" (or a rowless
    # orphan no longer present) by the time Phase 4 needs to decide. One
    # fresh, locked re-inventory here — not one per candidate, since
    # `containerlab inspect --all` is not cheap enough to run per instance —
    # replaces Phase 1's snapshot for every Phase 4 eligibility check below.
    fresh_runtime = engine.inventory()

    # Phase 4 — database projections/enqueues, after all required host
    # operations for this pass have completed. Re-fetch open-operation
    # membership fresh here rather than reusing Phase 2's — Phase 3's
    # destroys can each take minutes, plenty of time for a concurrent,
    # unrelated operation to appear against one of these instances.
    fresh_open_instance_ids = set(
        db.scalars(select(LabOperation.instance_id).where(LabOperation.state.in_(lab_operations.OPEN_STATES))).all()
    )

    queued = 0
    for instance in db_only_repairs:
        # Re-check this exact instance's current state immediately before
        # acting on it — Phase 2 only proved it looked eligible at snapshot
        # time. If a concurrent operation now has it live or already claimed,
        # something else is handling it and this pass must not interfere.
        # This is a cheap optimization to skip the obvious case, NOT the
        # correctness guarantee: a concurrent enqueue can still land after
        # this check and before the enqueue() call below, so instance fields
        # are only mutated once enqueue()'s own return value (the one
        # atomic, race-safe primitive here, backed by the database's partial
        # unique index) confirms the operation actually created is the one
        # this pass intended — never on the assumption that it was.
        db.refresh(instance)
        if (
            # Re-check the same escalation exclusion as Phase 2's list-build
            # above — a concurrent escalation could equally have landed
            # between the two.
            instance.status in ("provisioning", "active", "resetting", "error")
            or instance.id in fresh_open_instance_ids
            # Phase 1 said this name was running; if the fresh, post-Phase-3
            # inventory no longer shows it, something else already destroyed
            # it and there is nothing left here to enqueue a destroy for.
            or instance.instance_name not in fresh_runtime
        ):
            continue
        op = lab_operations.enqueue(
            db,
            instance=instance,
            kind="destroy",
            requested_by=None,
            origin="runtime_cleanup",
            runtime_precondition="present",
        )
        if op.kind != "destroy" or op.origin != "runtime_cleanup" or op.runtime_precondition != "present":
            # Lost the race to a genuinely concurrent operation (this pass's
            # OWN intended (kind, origin, runtime_precondition) tuple did not
            # come back) — enqueue() returned some other operation instead of
            # creating ours. Nothing was enqueued on this pass's behalf, and
            # nothing about this instance may be claimed as having happened.
            continue
        # This branch no longer projects repaired LIFECYCLE state itself —
        # only the worker's own locked, authoritative recheck
        # (lab_lifecycle.destroy_if_present, via the conditional operation
        # just enqueued) may settle status/error, since only it observes the
        # runtime under the host lock at execution time. See migration
        # 0060_lab_conditional_ops.py's module docstring for why: projecting
        # status/error here would assert an outcome this pass never verified
        # under lock, which is exactly the ordering bug this design closes.
        #
        # `fresh_runtime` (checked immediately above) is itself a fresh,
        # lock-verified OBSERVATION from this exact pass that the runtime
        # is genuinely present right now — recording it is not the kind of
        # unverified outcome-projection this design forbids; it is exactly
        # the "collectors/importers write facts" half of this codebase's
        # own source-of-truth standard. `status`/`error` remain the
        # worker's own conditional recheck's exclusive decision — those are
        # never touched here.
        instance.runtime_presence = "present"
        queued += 1

    for instance in missing_runtime:
        # Same re-validation, mirrored: a concurrent redeploy since Phase 2
        # would have left the instance live again and/or claimed by an open
        # operation, and must not be paved over with a stale destroy/deploy.
        # Same caveat as above: this pre-check is an optimization only, not
        # the correctness guarantee for the enqueue() branch below it.
        db.refresh(instance)
        if (
            instance.status not in ("provisioning", "active", "resetting")
            or instance.id in fresh_open_instance_ids
            # Phase 1 said this instance's runtime was missing; if the
            # fresh, post-Phase-3 inventory now shows it, a concurrent
            # deploy elsewhere already settled and this is no longer
            # missing — enqueueing a replay now would be a needless
            # destroy+redeploy cycle on a lab that just came back up.
            or instance.instance_name in fresh_runtime
        ):
            continue
        # A prior error no longer bypasses the worker's own conditional
        # recheck: it must ALSO go through the same conditional enqueue()
        # below as a fresh instance, not settle status="error" directly from
        # this snapshot. Only the worker's locked, authoritative observation
        # (lab_lifecycle.provision_if_absent, via the conditional operation
        # enqueued here) may decide the outcome.
        op = lab_operations.enqueue(
            db,
            instance=instance,
            kind="deploy",
            requested_by=None,
            origin="runtime_repair",
            runtime_precondition="absent",
        )
        if op.kind != "deploy" or op.origin != "runtime_repair" or op.runtime_precondition != "absent":
            # Same race as the destroy branch above: this pass's own intended
            # (kind, origin, runtime_precondition) tuple did not come back —
            # a genuinely concurrent operation won, so this pass enqueued
            # nothing and must not say otherwise.
            continue
        # This branch no longer projects repaired lifecycle state itself —
        # see the db_only_repairs branch's identical comment above for why.
        #
        # Same reasoning as db_only_repairs above: `fresh_runtime`'s
        # membership check immediately above is itself a lock-verified
        # observation that this instance's runtime is genuinely absent
        # right now.
        instance.runtime_presence = "absent"
        queued += 1

    db.flush()
    return queued, destroyed
