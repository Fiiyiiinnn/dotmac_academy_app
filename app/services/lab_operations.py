"""Durable lab-operation queue and worker ownership boundary.

Web callers only enqueue intents.  The lab worker claims one intent in a short
``FOR UPDATE SKIP LOCKED`` transaction, performs the external work, and settles
the row only while it still owns the claim.  A stale worker therefore cannot
publish database state after its lease has been reclaimed.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import case, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.config import LabExecutionPolicy
from app.models.lab import LabInstance, LabOperation, LabTemplate
from app.services import lab_lifecycle
from app.services.exceptions import ConflictError
from app.services.host_lock import HostLockUnavailable
from app.services.labengine.interface import LabEngine, WrongLabHostError

OPEN_STATES = ("queued", "claimed")
KINDS = ("deploy", "destroy", "check")
_CAPACITY_LOCK_KEY = 0x44414C  # "DAL"; one global Academy lab-capacity lock.
_BOOT_TOKEN = uuid4().hex[:8]
# Marks a `last_error` recorded for an operator-placement refusal (wrong lab
# host) rather than a genuine execution failure, so the automatic-destroy
# escalation count (a structural signal, not a message-substring guess about
# an engine's wording) can exclude these rows from the cumulative threshold.
_OPERATOR_REFUSAL_LAST_ERROR_PREFIX = "[operator-refusal] "
# Backward-compatibility fallback: a `WrongLabHostError`-caused failure that
# settled before `_OPERATOR_REFUSAL_LAST_ERROR_PREFIX` existed has no marker,
# just `str(exc)` verbatim. `ContainerlabEngine._require_lab_host` raises with
# this stable substring; matching it lets the escalation count exclude those
# legacy rows too, closing the gap between this fix and an earlier release.
_WRONG_LAB_HOST_LAST_ERROR_SUBSTRING = "containerlab operations require LAB_HOST_ROLE=lab"


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class WorkerIdentity:
    """Structural decomposition of one worker process's fencing identity.

    ``claimed_by`` (the composite ``host:epoch`` string) remains the sole
    value ever compared in a fencing predicate (``heartbeat``,
    ``_refresh_claim``, ``_settle``, ``run_claimed``'s staleness checks) —
    unchanged by this dataclass's existence. ``host``/``epoch`` exist only so
    a freshly started worker process can find rows it claimed under a
    *different* (crashed/replaced) incarnation of itself without parsing
    that composite string — see :func:`reclaim_previous_epoch`.
    """

    host: str
    epoch: str

    @property
    def claimed_by(self) -> str:
        return f"{self.host}:{self.epoch}"


def worker_identity_parts() -> WorkerIdentity:
    """Compute this worker process's identity.

    Cheap and deterministic for the whole process lifetime (hostname/pid/boot
    token never change once the process starts) — callers needing the value
    more than once (e.g. the CLI's startup reclaim call and its later
    ``claim_next`` calls) may call this repeatedly without risk of producing
    two different values, but should prefer computing it once and reusing
    the result within one call site.
    """
    return WorkerIdentity(host=socket.gethostname(), epoch=f"{os.getpid()}:{_BOOT_TOKEN}")


def worker_identity() -> str:
    """Process-unique identity used to fence one running worker incarnation.

    Must stay byte-identical to ``worker_identity_parts().claimed_by`` —
    existing rows/fencing predicates depend on this exact format.
    """
    return worker_identity_parts().claimed_by


def _insert_open_operation_stmt(
    *,
    instance: LabInstance,
    kind: str,
    requested_by: UUID | None,
    origin: str | None,
    runtime_precondition: str | None,
) -> Any:
    """Build one insert-or-skip statement for the open-per-instance constraint.

    The explicit value list is security-sensitive: the five base columns are
    exactly what migration 0055 grants ``app_user`` INSERT on. ``origin``/
    ``runtime_precondition`` (migration 0060) are worker-owned and excluded
    from that grant identically — they are only ever included in the values
    dict (never left implicit) when a caller actually passes them, so an
    ordinary web-tier enqueue (which never supplies them) sends exactly the
    same five-column INSERT it always has.
    """
    values: dict[str, object] = {
        "id": uuid4(),
        "tenant_id": instance.tenant_id,
        "instance_id": instance.id,
        "kind": kind,
        "requested_by": requested_by,
    }
    if origin is not None:
        values["origin"] = origin
    if runtime_precondition is not None:
        values["runtime_precondition"] = runtime_precondition
    return (
        insert(LabOperation)
        .values(**values)
        .on_conflict_do_nothing(
            index_elements=[LabOperation.instance_id],
            # Generic prepared plans cannot infer a partial index from bound
            # states. This closed schema predicate must stay literal (0055).
            index_where=text("state IN ('queued', 'claimed')"),
        )
        .returning(LabOperation.id)
    )


def _select_open_operation(db: Session, instance: LabInstance) -> LabOperation | None:
    """Return the current open (queued/claimed) operation for ``instance``, if any.

    Kept as its own call so a conflicting insert's re-check can observe the
    conflicting row settling out from under it between the insert and the
    re-select (see :func:`enqueue`).
    """
    return db.scalars(
        select(LabOperation).where(LabOperation.instance_id == instance.id).where(LabOperation.state.in_(OPEN_STATES))
    ).first()


def enqueue(
    db: Session,
    *,
    instance: LabInstance,
    kind: str,
    requested_by: UUID | None,
    origin: str | None = None,
    runtime_precondition: str | None = None,
) -> LabOperation:
    """Insert one open intent per instance, returning the existing one on races.

    Under READ COMMITTED, the conflicting operation can settle (leave
    ``OPEN_STATES``) in the gap between our insert's conflict and the re-select
    that follows it. When that happens the re-select legitimately finds
    nothing — retry the whole insert once, since the conflict should now be
    gone. If the retried insert conflicts again and the re-select once more
    finds nothing (the new conflicting row also settled in that same narrow
    window), give up with a clean, callable-facing error rather than raising
    ``NoResultFound``.

    ``origin``/``runtime_precondition`` (migration 0060) are optional and
    included in the insert only when a caller actually passes them (see
    ``_insert_open_operation_stmt``) — every ordinary caller
    (``lab_lifecycle.request_lab``, ``request_idle_reaps``,
    ``reconcile_stuck``, ``reclaim_previous_epoch``) leaves both ``None``,
    naming only the five columns ``app_user`` may ever INSERT.
    """
    if kind not in KINDS:
        raise ValueError(f"unsupported lab operation kind {kind!r}")
    inserted_id = db.scalar(
        _insert_open_operation_stmt(
            instance=instance,
            kind=kind,
            requested_by=requested_by,
            origin=origin,
            runtime_precondition=runtime_precondition,
        )
    )
    if inserted_id is not None:
        return db.scalars(select(LabOperation).where(LabOperation.id == inserted_id)).one()
    existing = _select_open_operation(db, instance)
    if existing is not None:
        return existing
    # The conflicting row settled between our insert-conflict and the
    # re-select above. Retry the complete insert exactly once.
    retried_id = db.scalar(
        _insert_open_operation_stmt(
            instance=instance,
            kind=kind,
            requested_by=requested_by,
            origin=origin,
            runtime_precondition=runtime_precondition,
        )
    )
    if retried_id is not None:
        return db.scalars(select(LabOperation).where(LabOperation.id == retried_id)).one()
    existing_after_retry = _select_open_operation(db, instance)
    if existing_after_retry is not None:
        return existing_after_retry
    raise ConflictError(
        f"could not enqueue {kind} operation for lab instance {instance.id}: "
        "a conflicting operation resolved during the enqueue retry"
    )


def claim_next(
    db: Session,
    *,
    claimed_by: str,
    claimed_host: str | None = None,
    claimed_epoch: str | None = None,
    policy: LabExecutionPolicy,
) -> LabOperation | None:
    """Claim the next ready operation; caller must commit immediately.

    ``claimed_by`` remains the sole fencing value. ``claimed_host``/
    ``claimed_epoch`` are optional structural ownership fields (see
    ``WorkerIdentity``/``reclaim_previous_epoch``) — every real worker claim
    site should pass both together (derived from the same
    ``WorkerIdentity``), but callers that only need `claimed_by`'s existing
    fencing behavior (most of this test suite) may omit them.
    """
    now = db.scalar(select(func.clock_timestamp()))
    op = db.scalars(
        select(LabOperation)
        .where(LabOperation.state == "queued")
        .where(LabOperation.not_before <= now)
        .order_by(
            case(
                (LabOperation.kind == "destroy", 0),
                (LabOperation.kind == "check", 1),
                else_=2,
            ),
            LabOperation.requested_at.asc(),
        )
        .execution_options(populate_existing=True)
        .with_for_update(skip_locked=True)
        .limit(1)
    ).first()
    if op is None:
        return None
    now = db.scalar(select(func.clock_timestamp()))
    op.claim_lease_seconds = policy.lease_seconds(op.kind)
    op.state = "claimed"
    op.claimed_by = claimed_by
    op.claimed_host = claimed_host
    op.claimed_epoch = claimed_epoch
    op.claimed_at = now
    op.heartbeat_at = now
    op.attempts += 1
    db.flush()
    return op


def heartbeat(db: Session, *, operation_id: UUID, claimed_by: str) -> bool:
    # Lock acquisition must COMPLETE before the separate clock statement.
    op = db.scalars(
        select(LabOperation)
        .where(LabOperation.id == operation_id)
        .where(LabOperation.state == "claimed")
        .where(LabOperation.claimed_by == claimed_by)
        .execution_options(populate_existing=True)
        .with_for_update()
    ).first()
    if op is None:
        return False
    _claimed_duration(op)
    now = db.scalar(select(func.clock_timestamp()))
    result = cast(
        CursorResult,
        db.execute(
            update(LabOperation)
            .where(LabOperation.id == operation_id)
            .where(LabOperation.state == "claimed")
            .where(LabOperation.claimed_by == claimed_by)
            .values(heartbeat_at=now)
        ),
    )
    return bool(result.rowcount)


def _claimed_duration(op: LabOperation) -> int:
    duration = op.claim_lease_seconds
    if type(duration) is not int or not 0 < duration <= 2147483647:
        raise ValueError("claimed lab operation has no valid granted lease")
    return duration


def _refresh_claim(db: Session, *, operation_id: UUID, claimed_by: str) -> None:
    """Refresh a lease at a phase boundary in its own short transaction."""
    with Session(db.get_bind()) as heartbeat_db:
        heartbeat_db.execute(text("SET LOCAL lock_timeout = '5s'"))
        alive = heartbeat(
            heartbeat_db,
            operation_id=operation_id,
            claimed_by=claimed_by,
        )
        heartbeat_db.commit()
    if not alive:
        raise RuntimeError("lab operation claim was lost before execution")


def _settle(
    db: Session,
    *,
    operation_id: UUID,
    claimed_by: str,
    state: str,
    error: str | None = None,
    refund_attempt: bool = False,
) -> bool:
    values: dict[str, object] = {
        "state": state,
        "heartbeat_at": _now(),
        "finished_at": _now(),
        "last_error": error,
    }
    if refund_attempt:
        values["attempts"] = func.greatest(LabOperation.attempts - 1, 0)
    result = cast(
        CursorResult,
        db.execute(
            update(LabOperation)
            .where(LabOperation.id == operation_id)
            .where(LabOperation.state == "claimed")
            .where(LabOperation.claimed_by == claimed_by)
            .values(**values)
        ),
    )
    return bool(result.rowcount)


def _clear_claim_fields(op: LabOperation) -> None:
    """Clear every claim/lease field together.

    Shared by every requeue path (capacity deferral, host-lock contention,
    ordinary lease recovery in ``reconcile_stuck``, and restart reclaim in
    ``reclaim_previous_epoch``) so ``claimed_host``/``claimed_epoch`` can
    never be left behind alongside a cleared ``claimed_by``, or vice versa —
    a stale structural-ownership pair surviving next to a fresh claim would
    make a future restart-reclaim scan misattribute the row. Terminal
    settlement (``_settle``, success or failure) does NOT call this: it
    deliberately retains these fields as audit provenance.
    """
    op.claim_lease_seconds = None
    op.claimed_by = None
    op.claimed_host = None
    op.claimed_epoch = None
    op.claimed_at = None
    op.heartbeat_at = None


def _project_requeued_instance_state(instance: LabInstance, kind: str) -> None:
    """Apply the standard "operation returned to the queue mid-flight"
    instance projection.

    Shared by ``reconcile_stuck``'s ordinary lease-expiry requeue and
    ``reclaim_previous_epoch``'s restart-reclaim requeue, so the two paths
    cannot drift apart over time: an under-ceiling ``provisioning``/
    ``resetting`` instance folds back to its pre-transition status, and an
    interrupted deploy/destroy can never prove the runtime absent.
    """
    if instance.status == "provisioning":
        instance.status = "queued"
    elif instance.status == "resetting":
        instance.status = "active"
    if kind in ("deploy", "destroy"):
        instance.runtime_presence = "unknown"


def _requeue_for_capacity(
    db: Session,
    op: LabOperation,
    instance: LabInstance,
    *,
    policy: LabExecutionPolicy,
) -> None:
    instance.status = "queued"
    op.state = "queued"
    _clear_claim_fields(op)
    op.not_before = _now() + timedelta(seconds=policy.lab_operation_retry_delay_seconds)
    op.last_error = None
    op.attempts = max(op.attempts - 1, 0)
    db.flush()


def _requeue_operation_after_host_lock(
    db: Session,
    op: LabOperation,
    *,
    policy: LabExecutionPolicy,
) -> None:
    """Return a claim's operation row to the queue after transient host-lock
    contention.

    Only touches the operation row. Callers decide separately whether the
    instance's status/error should be restored: for a "deploy" operation,
    ``_run_deploy`` may already have genuinely destroyed the old runtime
    before the lock contention was hit, so blindly restoring the pre-op
    instance state would be a false projection, not merely a conservative one.
    """
    op.state = "queued"
    _clear_claim_fields(op)
    op.not_before = _now() + timedelta(seconds=policy.lab_operation_retry_delay_seconds)
    op.last_error = None
    op.attempts = max(op.attempts - 1, 0)
    db.flush()


def _automatic_destroy_escalation_message(
    db: Session,
    *,
    instance_id: UUID,
    current_operation_id: UUID,
    current_operation_attempts: int,
    policy: LabExecutionPolicy,
) -> str | None:
    """Return an escalation message once cumulative automatic destroy
    ATTEMPTS for ``instance_id`` reach the destroy attempt ceiling, else
    ``None``.

    Sums ``attempts`` (not row count) across ``failed``, ``kind="destroy"``,
    ``requested_by IS NULL`` operations (automatic reaper-triggered destroys
    — user-initiated destroys pass a real person id) for this instance, plus
    the current failure's own ``attempts``. Summing attempts rather than
    counting rows matters because a single operation that hangs and burns its
    full attempt budget via lease-expiry (``reconcile_stuck`` reclaiming and
    re-attempting the same row) already represents up to
    ``policy.max_attempts("destroy")`` real attempts on its own — it must
    escalate at least as fast as that many separate single-attempt failures
    would, not need several more rows on top of it.

    Operator-placement refusals (``WrongLabHostError``, marked with
    ``_OPERATOR_REFUSAL_LAST_ERROR_PREFIX`` at settle time) are excluded: a
    misconfigured host role is not a genuine destroy execution failure. Rows
    settled before that marker existed (a stable substring of
    ``WrongLabHostError``'s own message, from
    ``ContainerlabEngine._require_lab_host``) are excluded the same way, so a
    gap between this fix landing and an earlier release of the worker leaves
    no unmarked wrong-host refusal able to count toward the threshold. Only
    failures after the instance's most recent *successful* ``deploy``
    operation count, so a successful manual redeploy starts a fresh window —
    but a ``deploy`` settled ``state="succeeded"`` with a non-``NULL``
    ``last_error`` is excluded from counting as that successful redeploy: the
    round-6 status-escalation fix settles a fully-ambiguous conditional
    deploy outcome as ``"succeeded"`` (the operation itself did not fail) while
    still marking the instance ``status="error"`` and the operation's own
    ``last_error``, so this is not a genuinely healthy redeploy and must not
    silently reset this window.
    The scan is bounded at the threshold rather than unbounded history: no
    single row can carry more attempts than the ceiling itself, so the most
    recent ``threshold`` failed rows are always enough to reach it if it can
    be reached at all.
    """
    threshold = policy.max_attempts("destroy")
    last_deploy_success_at = db.scalar(
        select(func.max(LabOperation.finished_at))
        .where(LabOperation.instance_id == instance_id)
        .where(LabOperation.kind == "deploy")
        .where(LabOperation.state == "succeeded")
        .where(LabOperation.last_error.is_(None))
    )
    prior_failure_query = (
        select(LabOperation.attempts)
        .where(LabOperation.instance_id == instance_id)
        .where(LabOperation.kind == "destroy")
        .where(LabOperation.state == "failed")
        .where(LabOperation.requested_by.is_(None))
        .where(LabOperation.id != current_operation_id)
        .where(
            or_(
                LabOperation.last_error.is_(None),
                ~LabOperation.last_error.startswith(_OPERATOR_REFUSAL_LAST_ERROR_PREFIX),
            )
        )
        .where(
            or_(
                LabOperation.last_error.is_(None),
                ~LabOperation.last_error.contains(_WRONG_LAB_HOST_LAST_ERROR_SUBSTRING, autoescape=True),
            )
        )
    )
    if last_deploy_success_at is not None:
        prior_failure_query = prior_failure_query.where(LabOperation.finished_at > last_deploy_success_at)
    bounded = prior_failure_query.order_by(LabOperation.finished_at.desc()).limit(threshold).subquery()
    prior_attempts = int(db.scalar(select(func.sum(bounded.c.attempts))) or 0)
    cumulative_attempts = min(prior_attempts + current_operation_attempts, threshold)
    if cumulative_attempts < threshold:
        return None
    return f"automatic destroy failed {cumulative_attempts} times; manual intervention required"


def _automatic_repair_deploy_escalation_message(
    db: Session,
    *,
    instance_id: UUID,
    current_operation_id: UUID,
    current_operation_attempts: int,
    policy: LabExecutionPolicy,
) -> str | None:
    """Return an escalation message once cumulative automatic repair-deploy
    ATTEMPTS for ``instance_id`` reach the deploy attempt ceiling, else
    ``None``.

    Mirrors :func:`_automatic_destroy_escalation_message` exactly, for the
    deploy side of the same problem: ``reconcile_runtime``'s
    ``missing_runtime`` branch manufactures a fresh ``LabOperation`` row
    (with a fresh attempt budget) every reconciler pass for as long as the
    instance's status stays in ``("provisioning", "active", "resetting")``
    and its runtime remains absent — nothing bounds this across passes,
    only within one row's own ``attempts``. Before this design, the
    reconciler itself capped this by escalating directly from its own
    snapshot the first time it saw a prior ``instance.error``; that
    projection was correctly removed (only the worker's own locked recheck
    may decide an outcome), but nothing replaced the bound it also
    provided. This closes that gap the same way the destroy side is
    already closed, from the worker's own settle-time observation, not a
    reconciler projection.

    Sums ``attempts`` across ``failed``, ``kind="deploy"``,
    ``origin="runtime_repair"``, ``requested_by IS NULL`` operations for
    this instance, plus the current failure's own ``attempts``. Excludes
    the same two operator-refusal/wrong-host cases
    ``_automatic_destroy_escalation_message`` excludes, for full symmetry
    with the destroy-side function: a ``WrongLabHostError`` from this exact
    code path is already fully handled by ``run_claimed``'s own earlier
    ``except WrongLabHostError`` clause (which restores state and refunds
    the attempt) before this generic failure handler ever runs, so no row
    settled through that clause can carry this branch's own state — but a
    wrong-host refusal that instead lease-expires or restart-reclaims
    before ever reaching that clause (and is later marked ``"failed"``
    directly by ``reconcile_stuck``/reclaim's own ceiling check) could
    otherwise still contribute here; a misconfigured host role is not a
    genuine repair-deploy execution failure either way. Only failures after
    the instance's most recent
    GENUINELY successful deploy count (``state="succeeded"`` AND
    ``last_error IS NULL`` — a "succeeded" settlement that only escalated
    an ambiguous conditional outcome to manual verification, see this
    file's round-9 fix to ``run_claimed``'s ``is_conditional_deploy``
    branch, does not count as a healthy redeploy and must not reset this
    window either).
    """
    threshold = policy.max_attempts("deploy")
    last_deploy_success_at = db.scalar(
        select(func.max(LabOperation.finished_at))
        .where(LabOperation.instance_id == instance_id)
        .where(LabOperation.kind == "deploy")
        .where(LabOperation.state == "succeeded")
        .where(LabOperation.last_error.is_(None))
    )
    prior_failure_query = (
        select(LabOperation.attempts)
        .where(LabOperation.instance_id == instance_id)
        .where(LabOperation.kind == "deploy")
        .where(LabOperation.origin == "runtime_repair")
        .where(LabOperation.state == "failed")
        .where(LabOperation.requested_by.is_(None))
        .where(LabOperation.id != current_operation_id)
        .where(
            or_(
                LabOperation.last_error.is_(None),
                ~LabOperation.last_error.startswith(_OPERATOR_REFUSAL_LAST_ERROR_PREFIX),
            )
        )
        .where(
            or_(
                LabOperation.last_error.is_(None),
                ~LabOperation.last_error.contains(_WRONG_LAB_HOST_LAST_ERROR_SUBSTRING, autoescape=True),
            )
        )
    )
    if last_deploy_success_at is not None:
        prior_failure_query = prior_failure_query.where(LabOperation.finished_at > last_deploy_success_at)
    bounded = prior_failure_query.order_by(LabOperation.finished_at.desc()).limit(threshold).subquery()
    prior_attempts = int(db.scalar(select(func.sum(bounded.c.attempts))) or 0)
    cumulative_attempts = min(prior_attempts + current_operation_attempts, threshold)
    if cumulative_attempts < threshold:
        return None
    return f"automatic repair deploy failed {cumulative_attempts} times; manual intervention required"


def _capacity_available(
    db: Session,
    instance: LabInstance,
    *,
    policy: LabExecutionPolicy,
) -> bool:
    """Serialize admission and evaluate the effective, global capacity limit.

    Capacity is now keyed off ``runtime_presence`` ("present" or "unknown"),
    not lifecycle ``status`` — see ``app/models/lab.py`` — because ``status``
    cannot simultaneously carry lifecycle/UI meaning and physical runtime
    existence. ``already_consuming`` preserves its exact prior semantic ("is
    THIS instance already counted, so admit it regardless of the cap"): it
    was ``instance.status in ("active", "resetting")`` when presence didn't
    exist; it is now the presence-based equivalent of that same "already
    capacity-counted" condition.
    """
    db.execute(select(func.pg_advisory_xact_lock(_CAPACITY_LOCK_KEY)))
    cfg = policy
    consuming = int(
        db.scalar(
            select(func.count())
            .select_from(LabInstance)
            .where(LabInstance.runtime_presence.in_(("present", "unknown")))
        )
        or 0
    )
    already_consuming = instance.runtime_presence in ("present", "unknown")
    if already_consuming:
        return True
    tenant_consuming = int(
        db.scalar(
            select(func.count())
            .select_from(LabInstance)
            .where(LabInstance.tenant_id == instance.tenant_id)
            .where(LabInstance.runtime_presence.in_(("present", "unknown")))
        )
        or 0
    )
    tenant_limit = cfg.max_concurrent_labs_per_tenant or cfg.max_concurrent_labs
    return consuming < cfg.max_concurrent_labs and tenant_consuming < tenant_limit


def _run_deploy(
    db: Session,
    instance: LabInstance,
    template: LabTemplate,
    engine: LabEngine,
    *,
    after_destroy: Callable[[], None] | None = None,
) -> None:
    """Idempotent destroy-then-deploy, preserving active state on destroy failure."""
    was_active = instance.status in ("active", "resetting")
    # The caller committed this reservation before engine work. Reasserting it
    # here is harmless and keeps the helper safe if called independently.
    instance.status = "resetting" if was_active else "provisioning"
    # Defensive restatement for standalone callers — the FIRST/authoritative
    # assignment of "unknown" for this deploy is made in run_claimed(), in the
    # same commit as the status reservation above, before this function ever
    # runs.
    instance.runtime_presence = "unknown"
    try:
        engine.destroy(instance.instance_name)
    except Exception as exc:
        # A failed destroy cannot prove the old runtime is absent. Count the
        # instance conservatively even for an initial/error retry so another
        # admission cannot consume the same capacity behind a leaked lab.
        instance.status = "active"
        instance.runtime_presence = "unknown"
        instance.error = str(exc)
        db.flush()
        raise
    if after_destroy is not None:
        after_destroy()
    lab_lifecycle.stop_consoles(instance)
    result = lab_lifecycle.provision(db, instance, engine, template)
    # ``provision`` may conservatively leave a failed deploy's status as
    # "active" (capacity-counted, unproven-absent runtime) rather than
    # "error" — its cleared-on-success ``error`` field is the authoritative
    # failure signal, not ``status`` alone.
    if result.status != "active" or result.error is not None:
        raise RuntimeError(result.error or "lab deployment failed")


def run_claimed(
    db: Session,
    *,
    operation_id: UUID,
    claimed_by: str,
    engine: LabEngine,
    policy: LabExecutionPolicy,
) -> str:
    """Execute and fenced-settle one claimed operation.

    Returns ``succeeded``, ``failed``, ``deferred``, or ``stale``.  The caller
    owns commit/rollback; a stale fence always rolls back database consequences.
    """
    op = db.get(LabOperation, operation_id)
    if op is None or op.state != "claimed" or op.claimed_by != claimed_by:
        db.rollback()
        return "stale"
    if _claimed_duration(op) != policy.lease_seconds(op.kind) or engine.execution_policy is not policy:
        raise ValueError("claimed operation and engine must share their granted policy")
    instance = db.get(LabInstance, op.instance_id)
    if instance is None:
        if not _settle(
            db,
            operation_id=operation_id,
            claimed_by=claimed_by,
            state="failed",
            error="lab instance no longer exists",
        ):
            db.rollback()
            return "stale"
        db.commit()
        return "failed"
    operation_kind = op.kind
    operation_instance_id = op.instance_id
    operation_requested_by = op.requested_by
    operation_attempts = op.attempts
    operation_origin = op.origin
    operation_runtime_precondition = op.runtime_precondition
    initial_instance_status = instance.status
    initial_instance_error = instance.error
    initial_instance_presence = instance.runtime_presence
    # Both must match (kind, origin, precondition) exactly — origin alone is
    # audit/provenance and is never a decision input on its own; see migration
    # 0060_lab_conditional_ops.py's module docstring for the full shape.
    is_conditional_deploy = (
        operation_kind == "deploy"
        and operation_origin == "runtime_repair"
        and operation_runtime_precondition == "absent"
    )
    is_conditional_destroy = (
        operation_kind == "destroy"
        and operation_origin == "runtime_cleanup"
        and operation_runtime_precondition == "present"
    )
    settle_error: str | None = None

    try:
        if operation_kind == "deploy":
            if is_conditional_deploy:
                # LOAD-BEARING ORDERING for the precondition-HOLDS (genuinely
                # absent) path: locked precondition check -> conditional
                # containerlab mutation (only if the precondition allows it) ->
                # console teardown (only if a mutation actually occurred) ->
                # console recreation / final projection. A precondition mismatch
                # must settle BEFORE engine.deploy/any topology mutation ever
                # runs — see migration 0060's module docstring. The mismatch
                # (runtime observed present) path is different: it always
                # tears down and resyncs consoles unconditionally — see
                # lab_lifecycle._rebuild_consoles_from_live_inspection's own
                # docstring for why.
                _refresh_claim(db, operation_id=operation_id, claimed_by=claimed_by)
                # Optimization only, NOT the correctness guarantee: an
                # already-known-present runtime may settle immediately as a
                # no-op without holding a capacity slot for an operation about
                # to no-op anyway. The mandatory, authoritative recheck is
                # inside lab_lifecycle.provision_if_absent()'s call to
                # engine.deploy_if_absent() below — a race that flips this
                # preliminary observation is still caught there.
                try:
                    preliminary_present = engine.status(instance.instance_name) == "running"
                except HostLockUnavailable:
                    raise
                if preliminary_present:
                    # This fully settles the outcome — deploy_if_absent() is
                    # NEVER called on this path — but through the same
                    # rigorous resync as any other confirmed-present outcome:
                    # a fresh, authoritative inspection runs FIRST, and only
                    # once it confirms a live handle are any stale consoles
                    # (routinely present for this operation kind's own target
                    # instance — see lab_lifecycle.resync_present_
                    # preliminary's own docstring) stopped and rebuilt. Never
                    # a bare trust-and-skip. No durable reservation is
                    # committed before this call: unlike an ordinary
                    # containerlab deploy, stop_consoles/start_console are
                    # idempotent/self-healing by construction, so the narrow
                    # claim-loss window this branch doesn't guard against
                    # (see lab_lifecycle.resync_present_preliminary's
                    # docstring) is an accepted residual property, not a gap.
                    lab_lifecycle.resync_present_preliminary(db, instance, engine)
                else:
                    if not _capacity_available(
                        db,
                        instance,
                        policy=policy,
                    ):
                        _requeue_for_capacity(
                            db,
                            op,
                            instance,
                            policy=policy,
                        )
                        db.commit()
                        return "deferred"
                    instance.status = "resetting" if instance.status in ("active", "resetting") else "provisioning"
                    instance.runtime_presence = "unknown"
                    db.commit()
                    refreshed_op = db.get(LabOperation, operation_id)
                    refreshed_instance = (
                        db.get(LabInstance, refreshed_op.instance_id) if refreshed_op is not None else None
                    )
                    if (
                        refreshed_op is None
                        or refreshed_instance is None
                        or refreshed_op.state != "claimed"
                        or refreshed_op.claimed_by != claimed_by
                    ):
                        db.rollback()
                        return "stale"
                    op = refreshed_op
                    instance = refreshed_instance
                    template = db.scalars(
                        select(LabTemplate)
                        .where(LabTemplate.tenant_id == instance.tenant_id)
                        .where(LabTemplate.activity_id == instance.activity_id)
                    ).one()
                    _refresh_claim(db, operation_id=operation_id, claimed_by=claimed_by)
                    lab_lifecycle.provision_if_absent(
                        db,
                        instance,
                        engine,
                        template,
                        preserved_status=initial_instance_status,
                        preserved_error=initial_instance_error,
                    )
                if instance.runtime_presence != "present":
                    # Neither resync_present_preliminary nor
                    # provision_if_absent's own ambiguous-outcome branches
                    # ever confirmed the runtime is genuinely present here
                    # — this settles "succeeded" at the shared tail below
                    # like any other non-exceptional outcome (an idempotent
                    # no-op is not a failure), but it must not be mistaken
                    # for a genuinely healthy redeploy by either escalation
                    # function's own window-reset query: mark the
                    # OPERATION's own last_error too, the only signal those
                    # queries have to distinguish "actually confirmed
                    # healthy" from "settled without ever confirming
                    # anything." This covers BOTH the escalated (status
                    # forced to "error") and non-escalated (status
                    # reverted/restored to its own prior value, e.g.
                    # "active") ambiguous sub-cases — checking `status ==
                    # "error"` alone missed the non-escalated sub-case
                    # entirely, since an "active" prior status is
                    # deliberately never escalated (it has its own safe
                    # self-healing path via reconcile_runtime's next pass)
                    # but is JUST AS UNCONFIRMED as the escalated case for
                    # the purpose of resetting either escalation window.
                    settle_error = (
                        f"{instance.error}; conditional deploy could not confirm runtime presence"
                        if instance.error
                        else "conditional deploy could not confirm runtime presence"
                    )
            elif not _capacity_available(
                db,
                instance,
                policy=policy,
            ):
                _requeue_for_capacity(
                    db,
                    op,
                    instance,
                    policy=policy,
                )
                db.commit()
                return "deferred"
            else:
                # Make the reservation visible and release the advisory
                # transaction lock before slow external work. A crash now
                # leaves a counted transient state that reconcile_stuck() can
                # repair and replay.
                instance.status = "resetting" if instance.status in ("active", "resetting") else "provisioning"
                # First/authoritative presence assignment for this deploy: this
                # is the durable reservation, committed before slow external
                # work, so a crash after this point must already treat the
                # runtime as uncertain rather than proven absent.
                instance.runtime_presence = "unknown"
                db.commit()
                refreshed_op = db.get(LabOperation, operation_id)
                refreshed_instance = db.get(LabInstance, refreshed_op.instance_id) if refreshed_op is not None else None
                if (
                    refreshed_op is None
                    or refreshed_instance is None
                    or refreshed_op.state != "claimed"
                    or refreshed_op.claimed_by != claimed_by
                ):
                    db.rollback()
                    return "stale"
                op = refreshed_op
                instance = refreshed_instance
                template = db.scalars(
                    select(LabTemplate)
                    .where(LabTemplate.tenant_id == instance.tenant_id)
                    .where(LabTemplate.activity_id == instance.activity_id)
                ).one()
                _refresh_claim(db, operation_id=operation_id, claimed_by=claimed_by)
                _run_deploy(
                    db,
                    instance,
                    template,
                    engine,
                    after_destroy=lambda: _refresh_claim(
                        db,
                        operation_id=operation_id,
                        claimed_by=claimed_by,
                    ),
                )
        elif operation_kind == "destroy":
            _refresh_claim(db, operation_id=operation_id, claimed_by=claimed_by)
            if is_conditional_destroy:
                # Same load-bearing ordering as the conditional-deploy branch
                # above, mirrored for destroy: precondition check -> mutation
                # (only if present) -> console teardown (only if destroyed).
                lab_lifecycle.destroy_if_present(db, instance, engine)
            else:
                # Durable reservation BEFORE slow external work, mirroring the
                # deploy path's commit above: once destroy begins the runtime's
                # mgmt IPs / ttyd ports may be released and reused, so make the
                # uncertainty (and the cleared console specs) visible to the web
                # tier immediately rather than at the end of the destroy. The
                # commit holds no lock we rely on (this branch takes neither the
                # capacity advisory lock nor a row lock beyond this instance's),
                # a later failure's rollback cannot undo it, and the existing
                # failure handler still re-asserts "unknown" afterwards.
                instance.runtime_presence = "unknown"
                instance.consoles = {}
                db.commit()
                lab_lifecycle.destroy(db, instance, engine)
        elif operation_kind == "check":
            template = db.scalars(
                select(LabTemplate)
                .where(LabTemplate.tenant_id == instance.tenant_id)
                .where(LabTemplate.activity_id == instance.activity_id)
            ).one()
            _refresh_claim(db, operation_id=operation_id, claimed_by=claimed_by)
            lab_lifecycle.grade(
                db,
                instance,
                engine,
                template,
                lab_lifecycle.handle_for(instance),
                operation_id=operation_id,
                claimed_by=claimed_by,
                before_each_check=lambda: _refresh_claim(
                    db,
                    operation_id=operation_id,
                    claimed_by=claimed_by,
                ),
            )
        else:  # Defensive against rows created outside the application.
            raise ValueError(f"unsupported lab operation kind {operation_kind!r}")
    except WrongLabHostError as exc:
        # Operator placement errors are not workload attempts and must not alter
        # learner-visible instance state. The engine increments the dedicated
        # refusal metric at the point it fails closed.
        db.rollback()
        unchanged_instance = db.get(LabInstance, operation_instance_id)
        if unchanged_instance is not None:
            unchanged_instance.status = initial_instance_status
            unchanged_instance.error = initial_instance_error
            # A host refusal proves no workload invocation occurred at all —
            # restore presence exactly as status/error are restored above.
            unchanged_instance.runtime_presence = initial_instance_presence
        if not _settle(
            db,
            operation_id=operation_id,
            claimed_by=claimed_by,
            state="failed",
            error=f"{_OPERATOR_REFUSAL_LAST_ERROR_PREFIX}{exc}",
            refund_attempt=True,
        ):
            db.rollback()
            return "stale"
        db.commit()
        return "failed"
    except HostLockUnavailable:
        # Another process holds the host containerlab lock. This is transient
        # host contention, not a workload failure or a fencing loss: refund
        # the attempt charged by claim_next and requeue the same row for a
        # prompt retry.
        db.rollback()
        unchanged_instance = db.get(LabInstance, operation_instance_id)
        refreshed_op = db.get(LabOperation, operation_id)
        if (
            unchanged_instance is None
            or refreshed_op is None
            or refreshed_op.state != "claimed"
            or refreshed_op.claimed_by != claimed_by
        ):
            db.rollback()
            return "stale"
        if is_conditional_deploy or is_conditional_destroy:
            # deploy_if_absent()/destroy_if_present() acquire host_lock
            # exactly ONCE for the whole observe-then-mutate operation — if
            # that single acquisition itself raises HostLockUnavailable,
            # NOTHING has been mutated yet (not even the precondition check
            # ran), unlike the ordinary destroy-then-deploy path below, which
            # cannot make that guarantee since it is two separate engine
            # calls. It is therefore safe — and more accurate, not merely
            # conservative — to restore the instance's exact pre-operation
            # state here, even for a conditional "deploy" whose capacity
            # reservation may have already committed a "resetting"/
            # "provisioning"/"unknown" placeholder in an earlier transaction.
            #
            # This restoration is equally safe on the OTHER conditional-
            # deploy path that can raise HostLockUnavailable here — the
            # preliminary-present branch's call into
            # lab_lifecycle.resync_present_preliminary() /
            # _rebuild_consoles_from_live_inspection(), which itself calls
            # engine.inspect_running() (also a single, atomic host_lock
            # acquisition). This is only true because
            # _rebuild_consoles_from_live_inspection() now calls
            # inspect_running() BEFORE stop_consoles() — previously it called
            # stop_consoles() first, so a HostLockUnavailable raised from
            # inspect_running()'s own lock acquisition could follow a
            # real, already-executed console teardown, making a blanket
            # "NOTHING has been mutated yet" claim overclaim in that case.
            # With that ordering fixed, no console has been touched either
            # by the time any HostLockUnavailable reaches this handler, so
            # restoring instance state verbatim is correct for both
            # call sites this branch covers.
            unchanged_instance.status = initial_instance_status
            unchanged_instance.error = initial_instance_error
            unchanged_instance.runtime_presence = initial_instance_presence
        elif operation_kind != "deploy":
            # Nothing destructive has necessarily happened yet for
            # destroy/check before this handler fires — safe to restore the
            # pre-execution instance state verbatim.
            unchanged_instance.status = initial_instance_status
            unchanged_instance.error = initial_instance_error
            unchanged_instance.runtime_presence = initial_instance_presence
        # For an ordinary (non-conditional) "deploy", _run_deploy already set
        # instance.status to "resetting"/"provisioning" for the whole
        # destroy-then-provision sequence before attempting the (possibly
        # already-succeeded) destroy. That value stays correctly
        # capacity-counted (_capacity_available treats provisioning/active/
        # resetting alike) and, unlike the pre-reset "active", does not
        # falsely claim a working lab when the real runtime may have just
        # been torn down and not yet redeployed. Leave it untouched. Presence
        # is likewise left as "unknown" (already set by _run_deploy/
        # run_claimed's reservation) rather than restored: an ordinary deploy
        # may have already destroyed the old runtime before contention hit
        # during the subsequent provision call, so blindly restoring the
        # pre-op presence would be a false projection. This is exactly the
        # ambiguity the conditional path above does NOT have.
        _requeue_operation_after_host_lock(
            db,
            refreshed_op,
            policy=policy,
        )
        db.commit()
        return "deferred"
    except Exception as exc:
        deploy_failure_status: str | None = None
        if operation_kind == "deploy":
            # Capture the intended failure projection before rollback. Active
            # includes resets and unproven destroys; every other origin is a
            # retryable error. Never commit partial DB consequences from the
            # failed execution merely to settle the operation.
            observed_status = instance.__dict__.get("status")
            deploy_failure_status = "active" if observed_status in ("active", "resetting") else "error"
            # Capture the presence observed just before rollback. A deploy
            # that fails after a successful destroy but before engine.deploy()
            # was ever invoked (e.g. topology preparation raises inside
            # provision()) already proved absence via provision()'s
            # pre-deploy-invocation failure path. Losing that signal here
            # would strand a proven-absent runtime as permanently
            # capacity-counted, since reconcile_runtime's repair branches
            # both exclude status="error" rows. This can only ever be
            # "absent" or "unknown" at this point: a successful deploy would
            # not have raised, so "present" is not reachable here.
            observed_presence = instance.__dict__.get("runtime_presence")
        db.rollback()
        if deploy_failure_status is not None:
            failed_instance = db.get(LabInstance, operation_instance_id)
            if failed_instance is not None:
                failed_instance.status = deploy_failure_status
                failed_instance.error = str(exc)
                # Preserve a proven "absent" observed just before rollback;
                # any other observed value (deploy was actually invoked and
                # failed, or the failure happened before provision() ever set
                # anything) stays conservatively "unknown" — a failed deploy
                # alone cannot otherwise prove absence.
                failed_instance.runtime_presence = "absent" if observed_presence == "absent" else "unknown"
                if operation_origin == "runtime_repair" and operation_requested_by is None:
                    # Automatic (reconciler-triggered) repair deploys retry
                    # forever otherwise — see this function's own escalation
                    # helper docstring for the full mechanism. Mirrors the
                    # destroy branch's own escalation exactly.
                    escalation_message = _automatic_repair_deploy_escalation_message(
                        db,
                        instance_id=operation_instance_id,
                        current_operation_id=operation_id,
                        current_operation_attempts=operation_attempts,
                        policy=policy,
                    )
                    if escalation_message is not None:
                        failed_instance.status = "error"
                        failed_instance.consoles = {}
                        failed_instance.error = escalation_message
                        # Never force "absent" here — escalation must not
                        # falsely claim the runtime is gone; presence stays
                        # whatever was just set above.
        elif operation_kind == "destroy":
            # A failed destroy (manual or automatic) cannot prove the runtime
            # is absent either — set this unconditionally, before the
            # automatic-only escalation check below, which never overrides
            # presence (see the destroy-escalation branch's comment).
            failed_instance = db.get(LabInstance, operation_instance_id)
            if failed_instance is not None:
                failed_instance.runtime_presence = "unknown"
            if operation_requested_by is None:
                # Automatic (reaper-triggered) destroys escalate after enough
                # cumulative failures instead of retrying forever;
                # user-initiated destroys (a real requested_by) are never
                # subject to this check.
                escalation_message = _automatic_destroy_escalation_message(
                    db,
                    instance_id=operation_instance_id,
                    current_operation_id=operation_id,
                    current_operation_attempts=operation_attempts,
                    policy=policy,
                )
                if escalation_message is not None:
                    escalated_instance = db.get(LabInstance, operation_instance_id)
                    if escalated_instance is not None:
                        escalated_instance.status = "error"
                        escalated_instance.consoles = {}
                        escalated_instance.error = escalation_message
                        # Never force "absent" here — escalation must not
                        # falsely claim the runtime is gone. Presence stays
                        # "unknown", already set above.
        if not _settle(
            db,
            operation_id=operation_id,
            claimed_by=claimed_by,
            state="failed",
            error=str(exc),
        ):
            db.rollback()
            return "stale"
        db.commit()
        return "failed"

    if not _settle(
        db,
        operation_id=operation_id,
        claimed_by=claimed_by,
        state="succeeded",
        error=settle_error,
    ):
        db.rollback()
        return "stale"
    db.commit()
    return "succeeded"


def _fail_operation_at_ceiling(
    db: Session,
    op: LabOperation,
    instance: LabInstance | None,
    *,
    now: datetime,
    last_error: str,
    policy: LabExecutionPolicy,
) -> None:
    """Mark ``op`` "failed" instead of requeuing it, because it has already
    reached its kind's attempt ceiling (``MAX_ATTEMPTS_BY_KIND``).

    Shared by ``reconcile_stuck``'s ordinary lease-expiry path and
    ``reclaim_previous_epoch``'s restart-reclaim path so the ceiling check and
    its instance projection — including automatic-destroy escalation and
    automatic repair-deploy escalation — cannot drift apart between the two
    recovery paths: a worker process that repeatedly crashes before settling
    an operation must eventually stop retrying exactly like a
    repeatedly-hanging one does. Each caller supplies its own ``last_error``
    message text; the projection/escalation logic itself is identical for
    both callers. Deliberately does NOT call ``_clear_claim_fields`` — like
    every other terminal settlement, the claim fields stay behind as audit
    provenance.
    """
    op.state = "failed"
    op.finished_at = now
    op.last_error = last_error
    if instance is not None and op.kind in ("deploy", "destroy"):
        # An interrupted external mutation cannot prove runtime absence.
        instance.status = "active"
        instance.error = op.last_error
        instance.runtime_presence = "unknown"
    if instance is not None and op.kind == "destroy" and op.requested_by is None:
        # A stuck/lease-expired/crash-reclaimed automatic destroy is the same
        # persistent-failure signal as a synchronous one — it must count
        # toward the same cumulative escalation threshold, or a hanging or
        # repeatedly-crashing engine lets the idle reaper re-enqueue destroys
        # for this instance forever.
        escalation_message = _automatic_destroy_escalation_message(
            db,
            instance_id=op.instance_id,
            current_operation_id=op.id,
            current_operation_attempts=op.attempts,
            policy=policy,
        )
        if escalation_message is not None:
            instance.status = "error"
            instance.consoles = {}
            instance.error = escalation_message
            # Presence stays "unknown" (set above) — never forced to
            # "absent" by escalation.
    if instance is not None and op.kind == "deploy" and op.origin == "runtime_repair" and op.requested_by is None:
        # Same reasoning as the destroy branch above, for automatic
        # repair-deploy retries: a stuck/lease-expired/crash-reclaimed
        # conditional deploy is the same persistent-failure signal as a
        # synchronous one (see run_claimed's own escalation wiring, round
        # 7) and must count toward the same cumulative threshold, or a
        # hanging or repeatedly-crashing engine lets reconcile_runtime's
        # missing_runtime branch re-enqueue repair deploys for this
        # instance forever.
        escalation_message = _automatic_repair_deploy_escalation_message(
            db,
            instance_id=op.instance_id,
            current_operation_id=op.id,
            current_operation_attempts=op.attempts,
            policy=policy,
        )
        if escalation_message is not None:
            instance.status = "error"
            instance.consoles = {}
            instance.error = escalation_message
            # Presence stays "unknown" (set above) — never forced to
            # "absent" by escalation.


def reconcile_stuck(db: Session, *, policy: LabExecutionPolicy) -> int:
    """Return expired claims and pre-cutover in-flight instances to the queue."""
    claimed = db.scalars(
        select(LabOperation)
        .where(LabOperation.state == "claimed")
        .execution_options(populate_existing=True)
        .with_for_update(skip_locked=True)
    ).all()
    # Validate every locked claim before effects, and sample time AFTER locks.
    durations = {op.id: _claimed_duration(op) for op in claimed}
    now = cast(datetime, db.scalar(select(func.clock_timestamp())))
    stuck = [
        op
        for op in claimed
        if (op.heartbeat_at or op.claimed_at or op.requested_at) < now - timedelta(seconds=durations[op.id])
    ]
    for op in stuck:
        instance = db.get(LabInstance, op.instance_id)
        if op.attempts >= policy.max_attempts(op.kind):
            _fail_operation_at_ceiling(
                db,
                op,
                instance,
                now=now,
                last_error=f"worker lease expired after {op.attempts} attempts",
                policy=policy,
            )
            continue
        if instance is not None:
            # The expired claim may have crossed the external mutation
            # boundary before the lease lapsed — _project_requeued_instance_state
            # sets/retains "unknown" for both requeue directions and for a
            # destroy under the ceiling (whose status stays "active" and hits
            # neither status branch).
            _project_requeued_instance_state(instance, op.kind)
        op.state = "queued"
        _clear_claim_fields(op)
        op.not_before = now
        op.last_error = "worker lease expired; operation requeued"
    # Phase-1-to-worker cutover: old queued/provisioning LabInstance rows have
    # no operation because the former worker scanned the instance table. Repair
    # them idempotently so rollout needs no manual data migration.
    open_operation = (
        select(LabOperation.id)
        .where(LabOperation.instance_id == LabInstance.id)
        .where(LabOperation.state.in_(OPEN_STATES))
        .exists()
    )
    missing = db.scalars(
        select(LabInstance)
        .where(LabInstance.status.in_(("queued", "provisioning", "resetting")))
        .where(~open_operation)
        .with_for_update(skip_locked=True)
    ).all()
    for instance in missing:
        instance.status = "active" if instance.status == "resetting" else "queued"
        # Provenance is ambiguous for these old, pre-cutover rows — set
        # "unknown" before enqueueing rather than trusting whatever presence
        # value (if any) they already carry.
        instance.runtime_presence = "unknown"
        enqueue(db, instance=instance, kind="deploy", requested_by=None)
    db.flush()
    return len(stuck) + len(missing)


def reclaim_previous_epoch(
    db: Session,
    *,
    host: str,
    epoch: str,
    policy: LabExecutionPolicy,
) -> int:
    """One-shot startup scan: reclaim every claim this same host took under a
    DIFFERENT (previous, presumably crashed-or-replaced) worker epoch.

    Intended to run exactly once, immediately after a worker acquires its
    startup singleton lock and before its ordinary polling loop begins — see
    ``app.cli._lab_worker``. This is strictly an acceleration of recovery for
    the narrow "this same host restarted" case; ``reconcile_stuck``'s
    ordinary lease-expiry recovery remains the general-purpose backstop and
    is unaffected and unduplicated by this function.

    Uses a plain ``FOR UPDATE`` — deliberately NOT ``SKIP LOCKED``. This scan
    runs once per worker process lifetime, not once per poll: a matching row
    skipped here because something else briefly held its lock would never be
    revisited by this mechanism again for the rest of this incarnation,
    silently degrading that one row to lease-expiry-only recovery — which
    defeats the point of running this scan at all. Paying a brief blocking
    wait once at startup is an acceptable cost that ``reconcile_stuck``'s
    repeated per-poll scan would not want to pay — but that wait is bounded,
    not unbounded: a ``lock_timeout`` is set on this same transaction before
    the ``FOR UPDATE`` query, so a conflicting lock held by some other
    transaction (most plausibly the separate ``lab-reconcile`` process, which
    is not covered by this worker's own singleton lock) makes this call raise
    rather than hang the whole worker's startup indefinitely. The caller
    (``app.cli._lab_worker``) already rolls back and re-raises any exception
    from this call, and a worker crashing on an unresolvable startup lock
    timeout — rather than hanging forever — is the correct, acceptable
    fail-fast behavior: ``Restart=always`` means systemd simply gives it a
    fresh attempt.

    A matched row already at its kind's attempt ceiling
    (``MAX_ATTEMPTS_BY_KIND``) is marked "failed" instead of requeued —
    exactly like ``reconcile_stuck``'s own ceiling check — via the same
    shared ``_fail_operation_at_ceiling`` helper. Below the ceiling, this
    requeue does NOT refund the attempt that ``claim_next`` charged — it
    leaves ``op.attempts`` exactly as-is, identically to ``reconcile_stuck``'s
    own under-ceiling requeue. A refund here would let a worker that
    repeatedly crashes before ever settling the same row cycle claim (+1)
    then reclaim (-1) forever without ``attempts`` ever net-advancing, so the
    ceiling and automatic-destroy escalation this codebase treats as
    load-bearing would never be reached for exactly the failure mode
    restart-reclaim exists to handle. Treating a restart-reclaimed,
    under-ceiling row exactly like an ordinary lease-timeout requeue for
    attempt-accounting purposes is what lets repeated crashes on the same
    operation correctly accumulate toward the ceiling instead of being
    unconditionally forgiven on every restart.

    Never touches a different host's claims, or a row with a null/legacy
    ``claimed_host``/``claimed_epoch`` (pre-0059 rows, or rows claimed by a
    worker that predates migration 0059) — those remain lease-expiry-only,
    exactly as they were before this function existed.
    """
    db.execute(select(func.set_config("lock_timeout", f"{policy.lab_restart_reclaim_lock_timeout_seconds}s", True)))
    matched = db.scalars(
        select(LabOperation)
        .where(LabOperation.state == "claimed")
        .where(LabOperation.claimed_host == host)
        .where(LabOperation.claimed_epoch.is_not(None))
        .where(LabOperation.claimed_epoch != epoch)
        .execution_options(populate_existing=True)
        .with_for_update()
    ).all()
    for op in matched:
        _claimed_duration(op)
    now = cast(datetime, db.scalar(select(func.clock_timestamp())))
    for op in matched:
        instance = db.get(LabInstance, op.instance_id)
        old_epoch = op.claimed_epoch
        if op.attempts >= policy.max_attempts(op.kind):
            _fail_operation_at_ceiling(
                db,
                op,
                instance,
                now=now,
                last_error=(
                    f"restart reclaim: previous worker epoch {old_epoch} "
                    f"superseded by {epoch} on {host}, but already at "
                    f"attempt ceiling after {op.attempts} attempts"
                ),
                policy=policy,
            )
            continue
        if instance is not None:
            _project_requeued_instance_state(instance, op.kind)
        op.state = "queued"
        _clear_claim_fields(op)
        op.not_before = now
        op.last_error = f"restart reclaim: previous worker epoch {old_epoch} superseded by {epoch} on {host}"
    db.flush()
    return len(matched)
