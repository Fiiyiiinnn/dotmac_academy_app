"""Lab lifecycle service (Task 6): quota-gated request, provision, grade→ledger.

The grade path runs product live checks and invokes the fenced Academy capability
in the caller's transaction.

Inc1 rule: these functions take ``db`` and ``flush`` only — they NEVER
``commit``. The request handler / CLI owns the transaction boundary.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import shutil
import signal
import socket
import subprocess
from collections.abc import Callable
from datetime import datetime, timezone
from uuid import UUID, uuid4

from dotmac_learning.models import Activity
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.lab import LabInstance, LabTemplate
from app.services.checks.engine import run_checks
from app.services.exceptions import ConflictError
from app.services.host_lock import HostLockUnavailable
from app.services.lab_result_capability import LabGradeReceipt, record_claimed_lab_check
from app.services.lab_seed import generate_seed, interpolate
from app.services.labengine.interface import LabEngine, LabHandle

#: URL prefix under which every console is served. Defined once: it is both the
#: ttyd ``-b`` base path and the pattern the orphan sweep matches on.
_CONSOLE_BASE = "/labs/instances/"

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _free_port() -> int:
    """Pick an ephemeral free localhost port (race-tolerant best effort)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _is_linux_kind(kind) -> bool:
    """Linux lab nodes get a ttyd terminal; RouterOS (vr-*) use webfig instead."""
    return not str(kind or "").startswith("vr")


def console_bind_host() -> str:
    """Address a locally spawned ttyd binds — see ``LAB_CONSOLE_BIND_HOST``."""
    return settings.lab_console_bind_host or settings.lab_console_host


def _is_local_address(host: str) -> bool:
    """True when this host owns ``host`` and ttyd could actually bind it.

    ttyd (libwebsockets) does NOT exit when its ``-i`` address is not local: it
    retries the bind in a tight loop, burning a whole core and never listening.
    Probing the bind ourselves turns that silent runaway into a refusal we log.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((host, 0))
        return True
    except OSError:
        return False


def start_console(cname: str, base_path: str) -> int | None:
    """Launch a ttyd browser terminal for a Linux container; return its port.

    ttyd serves an HTTP page + a WebSocket on ``<bind host>:<port>``. ``-b`` sets
    the URL base path so ttyd's internal links (index, /token, /ws) resolve under
    the auth-gated app proxy path; ``-W`` makes the terminal writable.

    Tolerant by design: if ttyd is missing, the bind address is not local, or the
    launch fails we log and return ``None`` so a console problem never blocks
    provisioning.

    ttyd is deliberately an ordinary child of the worker process, in the
    worker's own cgroup. A worker stop/restart therefore kills its consoles
    too. The continuous worker repairs that expected consequence before
    draining more operations: ``lab_jobs.recover_missing_consoles`` compares
    the process table with active/present instances, obtains a fresh
    ownership-checked runtime handle, preserves healthy node consoles, and
    starts only missing Linux-node consoles. A missing or failed inspection
    never changes the database projection or starts a process.
    """
    if shutil.which("ttyd") is None:
        logger.warning("ttyd not installed; skipping console for %s", cname)
        return None
    bind_host = console_bind_host()
    if not _is_local_address(bind_host):
        # Refusing is the whole point: spawning here would leak a process that
        # spins on an impossible bind until someone kills it by hand.
        logger.error(
            "console bind host %s is not local to this machine; refusing to spawn "
            "ttyd for %s (set LAB_CONSOLE_BIND_HOST, or run the lab worker on the "
            "host that owns that address)",
            bind_host,
            cname,
        )
        return None
    port = _free_port()
    argv = [
        "ttyd",
        "-p",
        str(port),
        "-i",
        bind_host,
        "-b",
        base_path,
        "-W",
        "docker",
        "exec",
        "-it",
        cname,
        "sh",
    ]
    try:
        subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:  # never fail provision on a console launch error
        logger.warning("ttyd launch failed for %s: %s", cname, exc)
        return None
    return port


def console_processes(
    instance_id=None,
) -> dict[str, dict[str, list[tuple[int, int | None]]]] | None:
    """Map instance id -> node -> ``(pid, port)`` for running ttyd consoles.

    ``port`` is ``None`` only for a matching process whose command line cannot
    be parsed completely.  The PID is still retained so orphan cleanup and
    duplicate-prevention never lose sight of a real ttyd process merely
    because its arguments are malformed or from an older launcher. The whole
    result is ``None`` when the process table cannot be scanned reliably;
    callers that might create processes must distinguish that from no matches.
    """
    scope = str(instance_id) if instance_id is not None else r"[0-9a-f-]+"
    pattern = rf"ttyd .* -b {_CONSOLE_BASE}{scope}/console/"
    try:
        proc = subprocess.run(["pgrep", "-af", pattern], check=False, capture_output=True, text=True)
    except Exception as exc:
        logger.warning("console process scan failed: %s", exc)
        return None
    if proc.returncode not in (0, 1):
        logger.warning("console process scan failed with exit code %s", proc.returncode)
        return None
    found: dict[str, dict[str, list[tuple[int, int | None]]]] = {}
    for line in proc.stdout.splitlines():
        pid_text, _, cmdline = line.partition(" ")
        match = re.search(rf"-b {_CONSOLE_BASE}([0-9a-f-]+)/console/([^\s]+)", cmdline)
        port_match = re.search(r"(?:^|\s)-p\s+(\d+)(?:\s|$)", cmdline)
        if match is None or not pid_text.isdigit():
            continue
        found.setdefault(match.group(1), {}).setdefault(match.group(2), []).append(
            (int(pid_text), int(port_match.group(1)) if port_match is not None else None)
        )
    return found


def console_pids(instance_id=None) -> dict[str, list[int]]:
    """Map instance id -> PIDs of the ttyd consoles currently serving it.

    Reads the process table via ``pgrep -af`` and parses the ``-b`` base path,
    which carries the instance id. Passing ``instance_id`` narrows the scan to
    one instance. Returns ``{}`` when nothing matches or the scan fails, which
    keeps teardown and orphan cleanup fail-closed.
    """
    processes = console_processes(instance_id)
    if processes is None:
        return {}
    return {
        instance: [pid for processes in nodes.values() for pid, _port in processes]
        for instance, nodes in processes.items()
    }


def stop_consoles(instance: LabInstance) -> int:
    """Kill the ttyd processes serving this instance's consoles; return the count.

    Returns a count rather than ``None`` so callers (and the orphan sweep) can log
    what actually happened — a silent no-op here is how consoles leaked before.
    """
    return kill_consoles(pid for pids in console_pids(instance.id).values() for pid in pids)


def kill_consoles(pids) -> int:
    """SIGTERM each console PID, tolerating one that already exited."""
    killed = 0
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            killed += 1
        except ProcessLookupError:
            continue  # already gone — nothing to do
        except OSError as exc:
            logger.warning("could not kill console pid %s: %s", pid, exc)
    return killed


def _set_topology_name(topology_text: str, name: str) -> str:
    """Force the containerlab topology ``name:`` to the unique per-trainee
    instance name, so concurrent deployments never collide and the container
    names (``clab-<name>-<node>``) match ``handle_for``."""
    if re.search(r"(?m)^\s*name:\s*.*$", topology_text):
        return re.sub(r"(?m)^\s*name:\s*.*$", f"name: {name}", topology_text, count=1)
    return f"name: {name}\n{topology_text}"


def handle_for(instance: LabInstance) -> LabHandle:
    """Reconstruct a :class:`LabHandle` from a live instance's recorded consoles.

    ``provision`` persists ``consoles[node] = {"kind", "mgmt"}``; container names
    follow containerlab's ``clab-<instance>-<node>`` convention, so we can rebuild
    the handle without re-inspecting the engine (used by the grade/check path).
    """
    consoles = instance.consoles or {}
    return LabHandle(
        instance_name=instance.instance_name,
        nodes={node: f"clab-{instance.instance_name}-{node}" for node in consoles},
        mgmt={node: c.get("mgmt") for node, c in consoles.items()},
        kinds={node: c.get("kind") for node, c in consoles.items()},
    )


def instance_name(instance_id) -> str:
    """Runtime name: ``dal-<canonical-hyphenated-uuid>``.

    The instance's own id is the whole name — global uniqueness follows
    directly from ``id`` being a UUID, backed by the DB-level unique index on
    ``instance_name`` (defense in depth: containerlab's runtime namespace is
    host-global, not tenant-scoped, so a tenant-scoped uniqueness guarantee on
    ``id`` alone would not be sufficient for the runtime name it derives).
    """
    return f"dal-{instance_id}"


def _attempt_seed_id(person_id, activity_id, n: int) -> int:
    """Deterministic 32-bit PRNG seed, stable per (person, activity, attempt)."""
    h = hashlib.sha256(f"{person_id}:{activity_id}:{n}".encode()).hexdigest()
    return int(h[:8], 16)


def _launch_lock_key(tenant_id, person_id, activity_id) -> int:
    """Stable signed bigint key for one learner/activity launch stream."""
    digest = hashlib.sha256(f"lab-launch:{tenant_id}:{person_id}:{activity_id}".encode()).digest()
    unsigned = int.from_bytes(digest[:8], "big")
    return unsigned if unsigned < (1 << 63) else unsigned - (1 << 64)


def request_lab(db: Session, *, tenant_id, person_id, activity: Activity, template: LabTemplate) -> LabInstance:
    """Create a LabInstance for the next attempt — does NOT deploy.

    Always queued for the worker-owned admission path. Concurrent or retried
    launches for the same learner/activity serialize and reuse the current
    non-reaped instance. Seed generation, instance creation, and the deploy
    intent otherwise occur atomically in the same request transaction.
    """
    db.execute(select(func.pg_advisory_xact_lock(_launch_lock_key(tenant_id, person_id, activity.id))))
    current = db.scalars(
        select(LabInstance)
        .where(LabInstance.tenant_id == tenant_id)
        .where(LabInstance.activity_id == activity.id)
        .where(LabInstance.person_id == person_id)
        .where(LabInstance.status != "reaped")
        .order_by(LabInstance.created_at.desc())
    ).first()
    if current is not None:
        return current

    prev = db.scalar(
        select(func.count())
        .select_from(LabInstance)
        .where(LabInstance.tenant_id == tenant_id)
        .where(LabInstance.activity_id == activity.id)
        .where(LabInstance.person_id == person_id)
    )
    n = int(prev or 0) + 1
    seed = generate_seed(template.seed_spec, attempt_id=_attempt_seed_id(person_id, activity.id, n))
    # Allocated explicitly (rather than left to uuid_pk()'s column default)
    # because the runtime name is derived from it below, before the row is
    # ever flushed — SQLAlchemy applies column defaults at INSERT/flush time,
    # not at ordinary Python object construction, so instance.id would still
    # be None here if read off the LabInstance() object instead.
    instance_id = uuid4()
    inst = LabInstance(
        id=instance_id,
        tenant_id=tenant_id,
        activity_id=activity.id,
        person_id=person_id,
        instance_name=instance_name(instance_id),
        seed=seed,
    )
    db.add(inst)
    db.flush()
    from app.services.lab_operations import enqueue

    enqueue(db, instance=inst, kind="deploy", requested_by=person_id)
    from app.services import learning_events

    learning_events.emit(
        db,
        tenant_id=tenant_id,
        person_id=person_id,
        kind="lab_launched",
        course_id=activity.course_id,
        subject_id=activity.id,
        detail={"instance": str(inst.id)},
    )
    return inst


def provision(db: Session, instance: LabInstance, engine: LabEngine, template: LabTemplate) -> LabInstance:
    """Deploy the topology for ``instance`` and record consoles / activate it.

    A failure BEFORE ``engine.deploy()`` is ever invoked (topology
    interpolation, name substitution) proves no runtime was created, so it is
    recorded as ``error``. A failure AT OR AFTER the ``engine.deploy()`` call
    does not prove the runtime is absent — the underlying containerlab
    process may still be running in the background — so the instance
    conservatively stays ``active`` instead, mirroring ``_run_deploy``'s
    destroy-failure handling in ``lab_operations.py``. Capacity accounting
    (``_capacity_available``) is presence-based, not status-based: this
    failure path sets ``instance.runtime_presence = "unknown"`` (set the
    moment invocation is reached, see below), which keeps it capacity-counted
    exactly like a genuinely active instance would be, and over-counting a
    phantom-but-live instance self-heals via ``reconcile_runtime`` once
    inventory proves absence, whereas under-counting would not self-correct
    as safely. ``instance.error`` is set to a non-``None`` message on every
    failure path — callers (``_run_deploy``) key off it, not just ``status``,
    to detect a failed deploy now that a failure can leave ``status ==
    "active"``. ``HostLockUnavailable`` is transient host-lock contention, not
    a deployment failure, and is re-raised unchanged rather than recorded here.
    """
    try:
        topology_text = _set_topology_name(interpolate(template.topology, instance.seed), instance.instance_name)
    except Exception as exc:  # nothing was ever attempted; no runtime to protect
        instance.status = "error"
        instance.error = str(exc)
        instance.runtime_presence = "absent"
        db.flush()
        return instance

    # Set immediately before invocation — an exception at or after this point
    # does not prove the runtime is absent.
    instance.runtime_presence = "unknown"
    try:
        handle = engine.deploy(topology_text, instance.instance_name)
        consoles: dict = {}
        for node in handle.nodes:
            kind = handle.kinds.get(node)
            spec = {"kind": kind, "mgmt": handle.mgmt.get(node)}
            if _is_linux_kind(kind):
                spec["port"] = start_console(
                    handle.nodes[node],
                    f"{_CONSOLE_BASE}{instance.id}/console/{node}",
                )
            consoles[node] = spec
        instance.consoles = consoles
        instance.status = "active"
        now = _now()
        instance.started_at = now
        instance.last_active_at = now
        instance.error = None
        instance.runtime_presence = "present"
    except HostLockUnavailable:
        raise
    except Exception as exc:  # deploy was invoked; failure doesn't prove absence
        instance.status = "active"
        instance.error = str(exc)
        instance.runtime_presence = "unknown"
    db.flush()
    return instance


def _build_consoles_from_handle(instance: LabInstance, handle: LabHandle) -> dict:
    """Shared node/console-spec construction — the same shape ``provision``,
    ``reset``, and the conditional deploy paths below all build from a fresh
    :class:`LabHandle`."""
    consoles: dict = {}
    for node in handle.nodes:
        kind = handle.kinds.get(node)
        spec = {"kind": kind, "mgmt": handle.mgmt.get(node)}
        if _is_linux_kind(kind):
            spec["port"] = start_console(
                handle.nodes[node],
                f"{_CONSOLE_BASE}{instance.id}/console/{node}",
            )
        consoles[node] = spec
    return consoles


def _rebuild_consoles_from_live_inspection(db: Session, instance: LabInstance, engine: LabEngine) -> None:
    """Resync ``consoles``/``status``/``error`` for a conditional deploy
    whose runtime is observed present, UNCONDITIONALLY — never gated on
    whether ``consoles`` already looks populated.

    A conditional ("runtime_repair") deploy's own TARGET instance is, by
    construction, one ``reconcile_runtime``'s ``missing_runtime`` branch
    flagged specifically because its lifecycle ``status`` said active/
    provisioning/resetting while its RUNTIME WAS MISSING — such an instance
    routinely already has NON-EMPTY ``consoles`` on it, recorded from
    whenever it genuinely WAS running before the runtime disappeared. That
    recorded data is stale (referring to a now-dead deployment) BEFORE this
    conditional-deploy operation even begins, so "consoles already
    populated" can never be trusted as proof of a still-valid, live console
    session for a conditional deploy specifically — unlike an ordinary,
    unconditional deploy, there is no realistic concurrent-different-deploy
    case to protect here (the partial unique open-operation index already
    guarantees at most one open operation per instance, so an independent,
    genuinely concurrent redeploy of this SAME instance while this
    conditional op is claimed is not a real path).

    This also covers the narrower crash-then-retry gap: a worker crash
    between ``engine.deploy_if_absent()`` genuinely succeeding and this same
    attempt ever recording ``consoles``/``status`` leaves a real, running lab
    whose row shows a stale lifecycle status (and possibly stale, pre-repair
    consoles) once ``reclaim_previous_epoch`` requeues the interrupted
    operation (see ``app/services/lab_operations.py``'s conditional-deploy
    branch for the full sequence).

    A REAL, fresh, locked inspection (``engine.inspect_running`` — never a
    redeploy) runs FIRST, BEFORE any console is touched. ``stop_consoles``
    only runs once that call has already returned successfully WITH A REAL
    HANDLE — never before it, never if it raises, and never on a clean
    ``None`` either (see the later paragraph below: a clean ``None`` leaves
    ``consoles``/``status``/``error`` completely untouched instead).

    This ordering is load-bearing, not cosmetic: ``inspect_running`` ->
    ``ContainerlabEngine._inspect_running_handle_unlocked`` -> its own
    ``_inspect_all()`` is fallible for real, ordinary reasons — a non-zero
    ``containerlab inspect`` return code (subprocess failure, host
    contention, timeout) or invalid JSON output both raise ``RuntimeError``
    (see ``_inspect_all``'s own deliberate error handling). An earlier
    version of this function called ``stop_consoles`` unconditionally
    FIRST, reasoning that it was "safe and correct even if what is recorded
    is already stale/dead" — but that reasoning only accounted for
    ``inspect_running`` returning cleanly (a handle or ``None``), never for
    it raising. If it raised AFTER ``stop_consoles`` had already killed the
    real local ttyd console process(es) for this instance, that teardown
    could not be undone by any DB rollback: the operation would error out
    with consoles already destroyed and no way to rebuild them, and — since
    the runtime is genuinely PRESENT in this exact code path (that is why a
    resync was attempted rather than a deploy) — the instance would not be
    "missing_runtime" either, so neither reconciler repair branch
    (``db_only_repairs``/``missing_runtime`` in ``lab_jobs.py``) would ever
    pick it back up automatically. There was no self-healing path back to
    working consoles.

    Reordering so ``inspect_running`` runs first closes that gap while
    preserving every existing invariant this function relies on: teardown
    still happens before any rebuild, and only once a fresh handle is
    actually in hand. The only change is that a RAISE from the inspection
    itself now propagates before any console is touched, so nothing
    destructive happens and the existing operation-level error handling
    (retry/ceiling machinery in ``lab_operations.py``, unchanged by this
    fix) can safely retry later with consoles still intact.

    Then converges to exactly the same end state a normal successful
    deploy would reach — for every case: stale-non-empty, empty, and the
    rare already-fully-correct case, which is just harmlessly reconfirmed
    (at the minor cost of a brief console restart). If the fresh inspection
    unexpectedly disagrees (does not find the instance running after all —
    e.g. a genuine, separate destroy raced in in the meantime, or
    ``ContainerlabEngine.inspect_running``'s own topology-path ownership
    check refused an untrusted same-name match), presence is conservatively
    set to "unknown" rather than asserting a lifecycle state with no real
    console data behind it. On this ``handle is None`` outcome,
    ``consoles``/``status``/``error`` are now left COMPLETELY UNTOUCHED —
    not torn down — because the runtime is genuinely present on this code
    path (that is why a resync was attempted in the first place): if
    ``inspect_running`` cannot confirm a handle (a genuine race where the
    lab was destroyed in between; the engine's own topology-path ownership
    check legitimately refusing an unverifiable match; or the fresh
    inspection's own parser failing to reconstruct a handle for some other
    reason), we have no positive proof either way, so we must not destroy
    the one thing — an existing live console — that might still be
    correctly serving the instance, especially since no reconciler branch
    self-heals a ``runtime_presence="unknown"`` instance whose ``status``
    still looks active. ``stop_consoles`` therefore only runs once a fresh
    handle IS obtained, exactly mirroring ``provision_if_absent``'s own
    "teardown only after mutation/observation proven" principle — extended
    here to mean "only after a POSITIVE observation," not any observation.
    """
    handle = engine.inspect_running(instance.instance_name)
    if handle is None:
        instance.runtime_presence = "unknown"
        db.flush()
        return
    stop_consoles(instance)
    instance.consoles = _build_consoles_from_handle(instance, handle)
    instance.status = "active"
    now = _now()
    instance.started_at = now
    instance.last_active_at = now
    instance.error = None
    instance.runtime_presence = "present"
    db.flush()


def resync_present_preliminary(db: Session, instance: LabInstance, engine: LabEngine) -> LabInstance:
    """Conditional deploy's cheap preliminary check (``engine.status()``)
    found the runtime already present, before any capacity admission ever
    ran. "Present" fully settles the outcome here — ``deploy_if_absent`` is
    never called on this path — but it settles through the SAME rigorous
    resync as any other confirmed-present outcome (see
    :func:`_rebuild_consoles_from_live_inspection`): a fresh, authoritative
    inspection runs first, and only once it confirms a live handle are any
    stale consoles torn down and rebuilt from that live truth. Never a bare
    trust-and-skip of whatever ``consoles``/``status`` this row happened to
    already have.

    If that follow-up inspection cannot confirm presence either (fully
    ambiguous) and the ``status`` this would otherwise leave in place is
    itself one of the transient-looking values (``"queued"``/
    ``"provisioning"``/``"resetting"``), it is escalated to ``"error"``
    instead of left as-is — otherwise it would match ``reconcile_stuck``'s
    own stuck-row sweep trigger shape once this operation closes with no
    open operation left to protect it, forcing an unconditional
    destroy+redeploy this conditional design exists to prevent.

    This branch never commits a durable reservation placeholder before
    calling the real external actions below (``stop_consoles``/
    ``start_console``, inside ``_rebuild_consoles_from_live_inspection``) the
    way an ordinary mutation branch's "reserve capacity, then re-verify the
    claim" pattern does — deliberately, not by oversight. That pattern
    exists to protect a genuinely non-idempotent, non-safely-retriable
    action: an actual ``containerlab deploy`` cannot simply be re-run if the
    claim is lost mid-flight, which is exactly why ``deploy_if_absent``'s own
    precondition check exists. ``stop_consoles``/``start_console`` are not
    that: ``stop_consoles`` matches live processes by ``instance.id`` alone
    (``console_pids``), not by a specific PID/port, so a subsequent retry of
    this same reclaimed operation harmlessly cleans up and rebuilds
    regardless of what a prior, abandoned attempt left running. No capacity
    reservation is being made on this path either — the runtime was already
    observed present, so there is no admission decision to protect. If the
    claim were lost mid-flight (a narrow window bounded by ``_refresh_
    claim``'s own heartbeat reset plus at most two ~60s-timeout inspect
    calls), the final settlement's fencing check would roll back the
    in-memory DB projection while these external actions had already
    happened for real — an accepted residual property of this branch, not a
    defect, precisely because those actions are idempotent/self-healing by
    construction.
    """
    _rebuild_consoles_from_live_inspection(db, instance, engine)
    if instance.runtime_presence != "present" and instance.status in ("queued", "provisioning", "resetting"):
        # Leaving a transient-looking status unresolved here, with no open
        # operation once this settles, would match reconcile_stuck's own
        # stuck-row sweep trigger shape and force an UNCONDITIONAL
        # destroy+redeploy against a runtime this conditional operation
        # never actually confirmed either way — reproducing exactly the
        # hazard the round-5 fix (in provision_if_absent, below) closed for
        # its own call site. Escalate to a state that sweep does not match,
        # rather than leaving ambiguity looking like an ordinary in-flight
        # reservation.
        instance.error = (
            f"{instance.error}; conditional deploy could not confirm runtime presence; manual verification required"
            if instance.error
            else "conditional deploy could not confirm runtime presence; manual verification required"
        )
        instance.status = "error"
        db.flush()
    return instance


def provision_if_absent(
    db: Session,
    instance: LabInstance,
    engine: LabEngine,
    template: LabTemplate,
    *,
    preserved_status: str,
    preserved_error: str | None,
) -> LabInstance:
    """Conditional deploy for a reconciler-originated "runtime_repair" intent.

    LOAD-BEARING ORDERING (see migration ``0060_lab_conditional_ops.py``'s
    module docstring for the full design rationale — an earlier, unauthorized
    attempt at this exact slice got this ordering wrong) FOR THE PRECONDITION-
    HOLDS (genuinely absent) PATH — the actual mutation this operation may
    perform:

        locked precondition check
        -> conditional containerlab mutation, IF the precondition allows it
        -> console teardown, ONLY IF a mutation actually occurred
        -> console recreation / final state projection

    ``engine.deploy_if_absent()`` performs the precondition check AND the
    mutation atomically inside one host-lock acquisition (see
    ``ContainerlabEngine.deploy_if_absent``) — this function never calls
    ``stop_consoles``/``start_console``/any topology mutation before that
    call has already returned a real handle proving the mutation occurred.

    The PRECONDITION-MISMATCH (runtime observed present) path is different
    and does NOT follow "teardown only if a mutation occurred": see below —
    it always tears down and resyncs, because the observation itself (not a
    mutation this operation performed) is what authoritatively confirms the
    runtime's current state, and any previously-recorded consoles on this
    specific operation kind's target instance are untrustworthy regardless.

    A precondition MISMATCH (``deploy_if_absent`` returns ``None`` — the
    runtime was found genuinely present, whether by this call's own
    authoritative check or a caller's own earlier preliminary check racing
    against it) settles unconditionally through
    :func:`_rebuild_consoles_from_live_inspection` — see its own docstring
    for why any pre-existing ``consoles`` on THIS conditional deploy's own
    target instance are untrustworthy by construction (routinely stale data
    from before the runtime this operation is repairing ever disappeared),
    so there is no "preserve as-is" branch here: a fresh, authoritative
    inspection runs first, and only once it confirms a live handle is
    whatever is currently recorded stopped and the row rebuilt from that
    live truth.

    ``preserved_status``/``preserved_error`` ARE read on this path — this
    corrects an earlier version of this docstring which claimed they were
    "therefore no longer read on this path". They are read specifically to
    restore the row when that follow-up inspection ALSO cannot confirm
    presence (``runtime_presence`` still ``"unknown"`` afterward): the
    caller's earlier, already-committed capacity-admission transaction left
    ``instance.status`` at a transient reservation placeholder
    (``"resetting"``/``"provisioning"``) that no ordinary settle path ever
    produces on its own, and leaving it there would let
    ``reconcile_stuck``'s stuck-row sweep later force it through an
    unconditional, potentially destructive destroy+redeploy cycle against a
    runtime this conditional operation had correctly declined to touch. So
    the row is restored to its true pre-admission ``status``/``error``
    instead — UNLESS that true pre-admission ``status`` was itself already
    one of the transient-looking values (``"queued"``/``"provisioning"``/
    ``"resetting"`` — a legitimate starting point for a ``missing_runtime``
    repair target), in which case restoring it verbatim would return the
    row to exactly the same stuck-row sweep trigger shape, so it is
    escalated to ``"error"`` instead. They remain unused only on the
    confirmed-present sub-case, where
    :func:`_rebuild_consoles_from_live_inspection` itself already sets
    ``status="active"``/``error=None`` authoritatively.
    """
    topology_text = _set_topology_name(interpolate(template.topology, instance.seed), instance.instance_name)
    # Set immediately before invocation, mirroring provision()'s own
    # reasoning: an exception at or after this point does not prove the
    # runtime is absent.
    instance.runtime_presence = "unknown"
    handle = engine.deploy_if_absent(topology_text, instance.instance_name)
    if handle is None:
        _rebuild_consoles_from_live_inspection(db, instance, engine)
        if instance.runtime_presence != "present":
            # The follow-up inspection could not confirm presence either —
            # genuinely ambiguous, not a confirmed live runtime. The row
            # must not be left at capacity admission's transient
            # "resetting"/"provisioning" reservation placeholder: no
            # ordinary settle path ever produces that state, and
            # reconcile_stuck's own stuck-row sweep would otherwise force
            # it through an unconditional, potentially destructive
            # destroy+redeploy cycle later. Restore the instance to its
            # true pre-admission state instead.
            restored_status = preserved_status
            restored_error = preserved_error
            if restored_status in ("queued", "provisioning", "resetting"):
                # Restoring verbatim would return this row to
                # reconcile_stuck's own stuck-row sweep trigger shape — the
                # row's TRUE pre-operation status can legitimately already
                # be transient-looking (a missing_runtime repair target may
                # start at "provisioning"/"resetting", not only "active");
                # once this operation closes with no open operation left
                # to protect it, that sweep would force it through an
                # UNCONDITIONAL destroy+redeploy this conditional design
                # exists to prevent. Escalate instead of restoring a value
                # that was already unsafe before this operation began.
                restored_status = "error"
                restored_error = (
                    f"{restored_error}; conditional deploy could not confirm "
                    "runtime presence; manual verification required"
                    if restored_error
                    else "conditional deploy could not confirm runtime presence; manual verification required"
                )
            instance.status = restored_status
            instance.error = restored_error
            db.flush()
        return instance
    # Precondition held (genuinely absent) and deploy_if_absent has already
    # performed the real mutation atomically — console teardown/recreation
    # may only run now, after that mutation is proven to have happened.
    stop_consoles(instance)
    instance.consoles = _build_consoles_from_handle(instance, handle)
    instance.status = "active"
    now = _now()
    instance.started_at = now
    instance.last_active_at = now
    instance.error = None
    instance.runtime_presence = "present"
    db.flush()
    return instance


def destroy_if_present(db: Session, instance: LabInstance, engine: LabEngine) -> LabInstance:
    """Conditional destroy for a reconciler-originated "runtime_cleanup" intent.

    Mirrors :func:`provision_if_absent`'s load-bearing ordering exactly:
    locked precondition check -> conditional mutation (only if present) ->
    console teardown (only if a mutation actually occurred) -> final
    projection. ``engine.destroy_if_present()`` performs the precondition
    check and the mutation atomically inside one host-lock acquisition.

    A precondition MISMATCH (``destroy_if_present`` returns ``False`` — the
    runtime was found genuinely absent already) is the no-op path:
    ``status``/``error``/``consoles`` are left completely untouched, and only
    ``runtime_presence`` is refreshed to ``"absent"`` from this genuine
    observation. No call to ``stop_consoles`` or any other mutation happens
    on this path.
    """
    destroyed = engine.destroy_if_present(instance.instance_name)
    if not destroyed:
        instance.runtime_presence = "absent"
        db.flush()
        return instance
    stop_consoles(instance)
    # Console specs hold mgmt IPs / ttyd ports that other instances reuse once
    # this one is gone; never leave them on a non-live row.
    instance.consoles = {}
    instance.status = "reaped"
    instance.runtime_presence = "absent"
    db.flush()
    return instance


def grade(
    db: Session,
    instance: LabInstance,
    engine: LabEngine,
    template: LabTemplate,
    handle: LabHandle,
    *,
    operation_id: UUID,
    claimed_by: str,
    before_each_check: Callable[[], None] | None = None,
) -> LabGradeReceipt:
    """Check the live instance and delegate its result in the caller's transaction.

    Heartbeats finish before the capability takes its operation lock. The SQL
    fence derives identity and validates the result against the stored template.
    SQL owns the activity fallback threshold; the worker cannot read Learning.
    """
    if instance.status != "active" or instance.error is not None:
        detail = f"status {instance.status!r}" if instance.status != "active" else "an unresolved infrastructure error"
        raise ConflictError(f"cannot check a lab instance with {detail}")
    result = run_checks(
        template.checks,
        engine,
        handle,
        instance.seed,
        before_each=before_each_check,
    )
    score_val = result["score"]
    max_score = result["max_score"]
    fraction = (score_val / max_score) if max_score else 0.0

    receipt = record_claimed_lab_check(
        db,
        tenant_id=instance.tenant_id,
        operation_id=operation_id,
        claimed_by=claimed_by,
        per_item=result["per_check"],
    )
    threshold = template.limits.get("pass_threshold")
    totals_match = all(
        actual == expected or (math.isnan(actual) and math.isnan(expected))
        for actual, expected in ((receipt.score, float(score_val)), (receipt.max_score, float(max_score)))
    )
    if not totals_match or (threshold is not None and receipt.passed != (max_score > 0 and fraction >= threshold)):
        raise ConflictError("lab check result mismatch")
    instance.last_active_at = _now()
    return receipt


def reset(db: Session, instance: LabInstance, engine: LabEngine, template: LabTemplate) -> LabInstance:
    """Legacy direct reset helper retained until the separately gated removal phase.

    Web routes never call this helper: they enqueue a durable deploy operation,
    and the lab worker owns destroy-then-deploy, capacity admission, leases,
    and settlement. This guard still keeps any direct service caller from
    bypassing those boundaries while the compatibility helper remains.

    Only ``active`` or ``error`` (a previously-failed deploy, retryable) may
    be reset; every other status is refused up front, before any engine
    interaction. This is an allow-list, not just a reaped exclusion:
    ``queued``/``provisioning`` are the capacity-controlled deployment path
    owned by ``lab_operations.run_claimed()`` (which enforces the effective
    capacity limit before calling ``provision()``) — resetting a ``queued`` instance would
    deploy it immediately and bypass that cap entirely, and resetting a
    ``provisioning`` one would race the worker's own ``provision()`` call on
    the same row/work directory. ``reaped`` (already-destroyed) is refused
    because a successful reset unconditionally sets ``status="active"``,
    which would resurrect it. This guard lives here — not in the web route —
    so it applies to every current and future caller of ``reset()``, not just
    the one route that exists today.

    Mirrors ``provision``'s guarded-deploy shape and its topology preparation:
    ``_set_topology_name()`` is applied the same way so the redeployed
    container names still follow the ``clab-<instance>-<node>`` convention
    ``handle_for`` expects — a reset that skipped this could deploy under the
    wrong name and silently desync from every other reader of ``consoles``.
    An engine/interpolation failure is recorded onto the row
    (``status="error"``) rather than propagating out as an unhandled
    exception — a legacy direct caller gets a normal return in either outcome.
    Only the ``db.flush()`` itself is left
    unguarded, since a database/transaction failure is not something this
    function can meaningfully paper over.

    Old ttyd consoles are stopped BEFORE the fresh ones are started —
    mirroring ``destroy()``'s ordering — because ``stop_consoles()`` matches
    processes by ``instance.id`` alone (see ``console_pids()``), not by the
    specific port each one was launched on. Calling it after starting the new
    consoles would kill the ones just spawned for the same instance, since
    they're indistinguishable from the old ones by that pattern.

    On success, ``instance.consoles`` is rebuilt from the fresh
    :class:`LabHandle` ``engine.reset()`` returns (mgmt IPs and console ports
    can change on redeploy) — the same node/console-spawn loop ``provision``
    uses, so a "successful" reset never leaves stale console/mgmt data on the
    row.
    """
    if instance.status not in ("active", "error"):
        raise ConflictError(f"cannot reset a lab instance with status {instance.status!r}")
    try:
        stop_consoles(instance)
        topology_text = _set_topology_name(interpolate(template.topology, instance.seed), instance.instance_name)
        # Set immediately before invocation — an interpolation/console
        # failure above this line leaves the prior presence value untouched;
        # a failure at or after this line has already crossed into "unknown".
        instance.runtime_presence = "unknown"
        handle = engine.reset(topology_text, instance.instance_name)
        consoles: dict = {}
        for node in handle.nodes:
            kind = handle.kinds.get(node)
            spec = {"kind": kind, "mgmt": handle.mgmt.get(node)}
            if _is_linux_kind(kind):
                spec["port"] = start_console(
                    handle.nodes[node],
                    f"{_CONSOLE_BASE}{instance.id}/console/{node}",
                )
            consoles[node] = spec
        instance.consoles = consoles
        instance.status = "active"
        instance.last_active_at = _now()
        instance.error = None
        instance.runtime_presence = "present"
    except Exception as exc:  # surface any deploy failure onto the row
        instance.status = "error"
        instance.error = str(exc)
        # Old consoles were stopped above; do not keep stale mgmt/port specs.
        instance.consoles = {}
        # No presence assignment here: if engine.reset() was never invoked,
        # presence is untouched (still whatever it was before this call); if
        # invocation began, it is already "unknown" from the pre-invocation
        # assignment above.
    db.flush()
    return instance


def destroy(db: Session, instance: LabInstance, engine: LabEngine) -> LabInstance:
    """Destroy the underlying lab and mark the instance ``reaped``.

    A failure here (``engine.destroy`` raising) cannot prove the runtime is
    absent — but this function does not catch it: the outer ``run_claimed``
    caller in ``app/services/lab_operations.py`` already owns settling
    failures for this operation and reapplies ``unknown`` after its own
    rollback, so there is nothing to handle here beyond conservatively
    setting "unknown" before invocation.
    """
    # Set conservatively before invocation, mirroring provision()/reset().
    instance.runtime_presence = "unknown"
    engine.destroy(instance.instance_name)
    stop_consoles(instance)
    # Console specs hold mgmt IPs / ttyd ports that other instances reuse once
    # this one is gone; never leave them on a non-live row.
    instance.consoles = {}
    instance.status = "reaped"
    instance.runtime_presence = "absent"
    db.flush()
    return instance
