import ipaddress
import json
import os
import re
import signal
import socket
import subprocess
import time

import yaml

from app.config import LabExecutionPolicy, settings
from app.services.host_lock import host_lock

from .interface import ExecResult, LabEngine, LabHandle, WrongLabHostError

# containerlab needs root (netns/bridges); the app/worker run unprivileged, so
# invoke it via passwordless sudo. docker exec / ssh stay unprivileged.
_CLAB = ("sudo", "-n", "containerlab")

# How long to wait, after SIGTERM-ing an abandoned command's process group,
# before escalating to SIGKILL. Short on purpose: this only runs once a
# command has already blown its own (much longer) timeout or the call was
# otherwise cancelled, so there is no remaining budget to wait politely.

# How often to re-check whether the whole process group has exited while
# waiting out the grace period above. Short enough that the grace period
# isn't meaningfully overshot, long enough not to busy-loop.
_GROUP_LIVENESS_POLL_INTERVAL_SECONDS = 0.1

# Signals this module defers (via `signal.pthread_sigmask`) around its own
# short, risky critical sections — see `_run_contained` and
# `_terminate_process_group`. SIGTERM is the worker's own graceful-shutdown
# signal (see `app.cli._sigterm_raises_shutdown_requested`); SIGINT is
# included because a real terminal Ctrl+C (-> KeyboardInterrupt) can land in
# exactly the same windows, and `_run_contained`'s own docstring already
# promises cleanup for cancellation generally, not only for SIGTERM.
_DEFERRED_SIGNALS = {signal.SIGTERM, signal.SIGINT}


def _unblock_worker_signals_in_child() -> None:
    """Run in the child, after fork but before exec (see ``preexec_fn`` on
    the ``subprocess.Popen`` call in :func:`_run_contained`).

    A forked child inherits its parent's signal mask as an independent copy
    fixed at the moment of ``fork()`` — and ``exec()`` does not clear a
    blocked signal (only a custom handler's disposition resets to default on
    exec; the block/mask state itself persists specifically to support cases
    where that is intentional). So a child spawned while this module's own
    ``_DEFERRED_SIGNALS``-blocking critical sections happen to have
    SIGTERM/SIGINT blocked would otherwise inherit that block permanently —
    nothing in the child ever unblocks it, since the parent's own
    ``finally: unblock`` only ever affects the parent's mask going forward,
    not the child's already-forked, independent copy. That would silently
    defeat this whole module's "ask nicely (SIGTERM) first, then SIGKILL"
    escalation design for the child's entire lifetime: no SIGTERM sent to it
    by :func:`_terminate_process_group` could ever be delivered/acted on, so
    every cleanup would unconditionally fall through the full grace period to
    SIGKILL.

    Async-signal-safe: this does nothing but adjust the calling process's own
    signal mask, so it carries none of ``preexec_fn``'s usual multi-threaded
    deadlock risk (that risk comes from arbitrary other work happening in the
    child before exec — e.g. taking a lock also held by another thread of the
    forking parent — not from adjusting the signal mask itself). This is safe
    here specifically because this module's own production call path
    (``app.cli._lab_worker``) is single-threaded.
    """
    signal.pthread_sigmask(signal.SIG_UNBLOCK, _DEFERRED_SIGNALS)


def _terminate_process_group(proc: "subprocess.Popen[str]", *, grace_seconds: int) -> None:
    """Best-effort teardown of ``proc`` and every process it spawned.

    Every external command this module runs is started with
    ``start_new_session=True`` (see :func:`_run_contained`), which makes
    ``proc.pid`` the process-group leader's pid too — so signalling the
    *group* via :func:`os.killpg` reaches children the direct child itself
    spawned (e.g. containerlab's own worker subprocesses), not just the
    directly-spawned process. A plain ``proc.kill()`` would only ever reach
    the direct child and would leak any grandchildren that outlive it.

    SIGTERM first, then a bounded grace period, then SIGKILL if the group is
    still alive — the same escalation shape used everywhere else in this
    codebase for "ask nicely, then don't". ``ProcessLookupError`` at either
    signalling step means the group is already gone; that is success, not a
    failure to swallow silently into the caller's real exception.

    Caveat that cannot be verified from this repository: every command here
    is actually invoked as ``sudo -n containerlab ...`` (see ``_CLAB``, and
    ``docker exec``/``ssh`` for the non-containerlab call sites). Whether a
    signal delivered to the ``sudo``-spawned process group actually reaches
    containerlab's own descendants depends on the real lab host's sudoers
    and PAM/session configuration (e.g. whether sudo retains or drops the
    session, and whether it forwards signals to the command it execs). This
    module tests the process-group mechanics themselves against real,
    non-sudo subprocesses; it cannot prove signal propagation through the
    real host's ``sudo -n containerlab`` invocation from a test running here.
    That must be manually verified on the actual lab host as part of this
    rollout.

    The grace period is judged by whole-*group* liveness, never by
    ``proc.wait(timeout=...)`` alone. ``proc.wait()`` only waits for the
    direct child — the process-group leader — not the whole group. The
    direct child is never the one installing a SIGTERM handler here, so it
    routinely dies from SIGTERM almost immediately; if that alone ended the
    grace period, a grandchild doing its own graceful shutdown (e.g.
    containerlab's own cleanup subprocess tearing down network namespaces or
    bridges) would be cut off after a fraction of a second instead of the
    full grace period this function promises, risking host state left
    half-mutated. Group liveness is instead probed directly with
    ``os.killpg(pid, 0)`` — signal ``0`` delivers nothing but raises
    ``ProcessLookupError`` only once *every* process sharing that pgid has
    exited, so it stays true as long as any single member (direct child or
    any grandchild) is still alive. The loop below polls that probe on a
    bounded ``time.monotonic()`` deadline set ``grace_seconds``
    in the future, sleeping between polls via a short, bounded
    ``proc.wait(timeout=...)`` so the direct child's own zombie is reaped
    along the way without letting its exit end the grace period early —
    only the group-liveness probe reporting everything gone does that.
    SIGKILL fires only if the deadline is reached with the probe still
    showing something alive.

    Sending ``SIGKILL`` to a process group whose leader has already exited,
    but that still has surviving members, remains safe and correct — the
    pgid stays valid as long as any member is alive. Sending it to an
    already-fully-empty group just raises ``ProcessLookupError``, handled
    the same as the SIGTERM step above.

    The final reap of the direct child (after the SIGTERM/grace/SIGKILL
    sequence above has run) is itself bounded by
    ``grace_seconds``, not an unbounded ``proc.wait()``. SIGKILL
    cannot interrupt a process stuck inside an uninterruptible kernel sleep
    (D-state, e.g. from stuck I/O) — the kill is queued but only takes effect
    once that syscall returns, which can be indefinitely far in the future.
    This function runs while the worker still holds both the singleton lock
    and the per-operation host lock, so blocking here forever would prevent
    worker restart/reclaim forever too — the exact "no external command
    interaction may hang the worker indefinitely" property this module is
    built around. If the bounded wait still times out, there is nothing more
    user-space code can do about a process wedged in the kernel: this is a
    deliberate fail-stop/give-up policy, not a bug to fix further. The
    caller proceeds (and releases its locks) rather than holding every lock
    hostage on a single stuck process.

    The whole body runs with this thread's own delivery of
    ``_DEFERRED_SIGNALS`` (SIGTERM and SIGINT) blocked (via
    ``signal.pthread_sigmask``), unblocked again in a ``finally`` on every
    exit path. Without this, a second SIGTERM arriving while this function is
    already running — a realistic operator behaviour, since sending SIGTERM
    twice is exactly what someone does when the first one doesn't appear to
    have worked yet — would raise ``_WorkerShutdownRequested`` again
    mid-escalation (the handler installed by
    ``app.cli._sigterm_raises_shutdown_requested`` stays installed for the
    whole worker lifetime, including while this function runs), aborting the
    SIGTERM -> grace-period -> SIGKILL -> reap sequence before it has
    confirmed the group is actually dead. That would defeat the entire point
    of this cleanup path: the caller's outer exception handling would return
    normally, releasing locks, while the subprocess tree may still be alive.
    SIGINT is blocked alongside SIGTERM for the identical reason applied to a
    real terminal Ctrl+C (-> KeyboardInterrupt) landing mid-escalation instead
    of a second SIGTERM — this function's own docstring already promises
    cleanup for cancellation generally, not only for SIGTERM. Blocking defers
    delivery until the mask is lifted, at which point any signal that arrived
    while blocked is delivered immediately and the raise still happens once
    this function has finished — it is not lost, only postponed past the
    critical section.
    """
    signal.pthread_sigmask(signal.SIG_BLOCK, _DEFERRED_SIGNALS)
    try:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        else:
            deadline = time.monotonic() + grace_seconds
            group_gone = False
            while True:
                try:
                    os.killpg(proc.pid, 0)
                except ProcessLookupError:
                    group_gone = True
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                poll_budget = min(_GROUP_LIVENESS_POLL_INTERVAL_SECONDS, remaining)
                wait_started = time.monotonic()
                try:
                    proc.wait(timeout=poll_budget)
                except subprocess.TimeoutExpired:
                    pass
                # `Popen.wait()` caches the direct child's return code once it has
                # been reaped once, so every call after that returns instantly
                # instead of actually blocking for `poll_budget` — regardless of
                # whether it raises `TimeoutExpired`. The direct child (the
                # process-group leader) routinely dies from SIGTERM almost
                # immediately, so without this the loop degenerates into a
                # busy-spin hammering the killpg(pid, 0) probe for the rest of
                # the grace period. Sleep out whatever portion of the intended
                # poll interval `proc.wait()` didn't actually spend blocking,
                # bounded by whatever grace-period time remains, so each
                # iteration still takes roughly `poll_budget` of real wall-clock
                # time either way.
                elapsed = time.monotonic() - wait_started
                shortfall = poll_budget - elapsed
                if shortfall > 0:
                    remaining_after_wait = deadline - time.monotonic()
                    sleep_for = min(shortfall, remaining_after_wait)
                    if sleep_for > 0:
                        time.sleep(sleep_for)
            if not group_gone:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        try:
            proc.wait(timeout=grace_seconds)
        except Exception:  # noqa: S110 - best-effort reap; never mask the caller's real exception
            pass
    finally:
        signal.pthread_sigmask(signal.SIG_UNBLOCK, _DEFERRED_SIGNALS)


def _run_contained(
    cmd: list[str] | tuple[str, ...], *, timeout: float, grace_seconds: int
) -> "subprocess.CompletedProcess[str]":
    """Run ``cmd`` in its own process group, guaranteeing group teardown.

    Replaces a bare ``subprocess.run(...)`` at every containerlab/docker/ssh
    call site in this module. The returned value is shaped exactly like
    ``subprocess.run``'s ``CompletedProcess`` — every call site's existing
    ``.returncode``/``.stdout``/``.stderr`` handling, JSON parsing, and
    ``RuntimeError`` messages continue to work unchanged.

    On a timeout, or on any other exception (including cancellation such as
    ``KeyboardInterrupt``) reaching this function while the process is still
    running, the whole process group is torn down (see
    :func:`_terminate_process_group`) before the original exception is
    re-raised unchanged — a ``subprocess.TimeoutExpired`` stays a
    ``TimeoutExpired``, so existing callers' exception-handling contracts do
    not change.

    Delivery of ``_DEFERRED_SIGNALS`` (SIGTERM and SIGINT) to this thread is
    blocked (via ``signal.pthread_sigmask``) around ``subprocess.Popen(...)``
    itself. ``Popen.__init__`` forks+execs the child and then does further
    Python-level bookkeeping before returning; a SIGTERM or a real terminal
    Ctrl+C (-> KeyboardInterrupt, via SIGINT) landing anywhere in that window
    — including after the fork but before this function's own ``proc`` local
    is bound — would otherwise raise straight out of this call with no
    reference to the now-live, forked child anywhere. The ``except
    BaseException:`` cleanup below can only ever act on ``proc``, which would
    not exist yet in that window, so the child would leak permanently and
    untrackably. Blocking defers any such signal that arrives during
    construction until the mask is unblocked again.

    That unblock is deliberately performed inside the ``try`` guarding
    ``proc.communicate()``, immediately before the call, and NOT in a bare
    ``finally`` attached to the ``Popen(...)`` call. CPython's
    ``signal.pthread_sigmask(SIG_UNBLOCK, ...)`` does not merely flip the OS
    mask and return: if a signal in the set being unblocked is already
    pending (i.e. it was delivered while blocked), CPython synchronously
    invokes that signal's registered Python-level handler as part of the
    ``pthread_sigmask()`` call itself returning — before control returns to
    the calling bytecode. Concretely: ``signal.signal(signal.SIGTERM,
    handler); signal.pthread_sigmask(SIG_BLOCK, {SIGTERM});
    os.kill(os.getpid(), signal.SIGTERM); signal.pthread_sigmask(SIG_UNBLOCK,
    {SIGTERM})`` — the final call itself raises whatever ``handler`` raises.
    So if the unblock lived in a bare ``finally`` after ``Popen(...)``, a
    signal that arrived (and was deferred) during construction would be
    raised BY that ``finally`` clause, propagating straight out of this
    function without ever reaching the ``except BaseException:`` block below
    — the exact case this docstring says must be caught. Performing the
    unblock inside the ``try`` immediately above ``communicate()`` instead
    means any such re-raised signal is caught by the same ``except
    BaseException:`` that already handles a signal landing during
    ``communicate()`` itself, and ``_terminate_process_group(proc)`` runs
    correctly since ``proc`` is already bound by that point.

    ``Popen(...)`` itself can still fail before any child exists (e.g. a
    missing binary raising ``FileNotFoundError``) — that failure is caught
    separately, immediately around construction, purely to unblock the mask
    before re-raising. There is no process to tear down in that case, but
    leaving the mask blocked would otherwise cost this process
    SIGTERM/SIGINT responsiveness for the rest of its lifetime over an
    unrelated construction failure.

    The forked child itself must not inherit this blocked mask: a forked
    child's own copy of its signal mask is fixed at the moment of fork,
    independent of the parent's, and unblocking in the parent only ever
    affects the parent's mask going forward — never the child's. ``exec()``
    does not clear a blocked signal either. Left unhandled, every process
    this module spawns (containerlab, docker, ssh — and anything they
    themselves fork) would have SIGTERM (and SIGINT) permanently blocked from
    the moment of fork, so a SIGTERM sent to it by
    :func:`_terminate_process_group`'s escalation could never be
    delivered/acted on, defeating the entire "ask nicely first" design.
    ``preexec_fn=_unblock_worker_signals_in_child`` runs in the child, after
    fork but before exec, to unblock both signals there before the real
    command execs — see that function's own docstring for why this is safe
    despite ``preexec_fn``'s usual multi-threaded caveats.
    """
    signal.pthread_sigmask(signal.SIG_BLOCK, _DEFERRED_SIGNALS)
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            preexec_fn=_unblock_worker_signals_in_child,
        )
    except BaseException:
        # Popen() itself failed before any child exists to clean up — no
        # _terminate_process_group call is possible or needed, but the mask
        # must still be unblocked so this process doesn't lose SIGTERM/SIGINT
        # responsiveness for the rest of its lifetime.
        signal.pthread_sigmask(signal.SIG_UNBLOCK, _DEFERRED_SIGNALS)
        raise
    try:
        # Unblocking here, immediately before communicate() and inside this
        # try (not in a bare `finally` after Popen(...)), is deliberate: if a
        # signal was pending during construction, this very unblock call can
        # itself synchronously raise it (see the docstring above) — and
        # doing that inside this `try` means the `except` below still runs
        # `_terminate_process_group(proc)` correctly, since `proc` already
        # exists and is bound by this point.
        signal.pthread_sigmask(signal.SIG_UNBLOCK, _DEFERRED_SIGNALS)
        stdout, stderr = proc.communicate(timeout=timeout)
    except BaseException:
        _terminate_process_group(proc, grace_seconds=grace_seconds)
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def _requires_kvm(topology_text: str) -> bool:
    """Return whether parsed topology declares a vr-* node kind anywhere.

    Containerlab permits ``kind`` on a node, group, or ``defaults`` block.
    Walking the parsed document keeps this fail-closed as those inheritance
    forms evolve, without relying on YAML spelling or quoting.
    """
    topology = yaml.safe_load(topology_text) or {}

    def _walk(value: object) -> bool:
        if isinstance(value, dict):
            return any(
                (key == "kind" and isinstance(child, str) and child.startswith("vr-")) or _walk(child)
                for key, child in value.items()
            )
        if isinstance(value, list):
            return any(_walk(child) for child in value)
        return False

    return _walk(topology)


def _action_remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeError("RouterOS readiness exceeded the lab action deadline")
    return remaining


def _webfig_responding(address: ipaddress.IPv4Address | ipaddress.IPv6Address, deadline: float) -> bool:
    """Read only a bounded status line, with one absolute two-second I/O budget.

    Numeric addresses avoid DNS. Socket timeouts are refreshed before every I/O
    against the same deadline, so a trickling peer cannot extend the probe.
    Cancellation propagates after socket cleanup; ordinary network errors retry.
    """
    probe_deadline = min(deadline, time.monotonic() + 2)
    connection = None
    try:
        family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
        connection = socket.socket(family, socket.SOCK_STREAM)
        connection.settimeout(_action_remaining(probe_deadline))
        connection.connect((str(address), 80))
        host = f"[{address}]" if address.version == 6 else str(address)
        request = f"GET / HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode("ascii")
        connection.settimeout(_action_remaining(probe_deadline))
        connection.sendall(request)
        line = bytearray()
        while len(line) < 8192:
            connection.settimeout(_action_remaining(probe_deadline))
            chunk = connection.recv(1)
            if not chunk:
                return False
            line.extend(chunk)
            _action_remaining(probe_deadline)
            if line.endswith(b"\r\n"):
                return re.fullmatch(rb"HTTP/1\.[01] [1-4][0-9]{2}(?: [^\r\n]*)?\r\n", line) is not None
        return False
    except (OSError, RuntimeError):
        return False
    finally:
        if connection is not None:
            connection.close()


def _wait_for_routeros(handle: LabHandle, deadline: float) -> None:
    """Gate an owned handle within its existing deploy/inspect action budget.

    Presence-only inventory/status never call this helper. A timeout leaves the
    runtime intact and raises before lifecycle activation or console teardown.
    """
    pending = {}
    for node, kind in handle.kinds.items():
        if kind != "vr-ros":
            continue
        address = handle.mgmt.get(node)
        try:
            if not isinstance(address, str) or "%" in address:
                raise ValueError("numeric management address required")
            pending[node] = ipaddress.ip_address(address)
        except ValueError as exc:
            raise RuntimeError(f"RouterOS node {node!r} has no numeric management address") from exc
    while pending:
        _action_remaining(deadline)
        for node, address in list(pending.items()):
            _action_remaining(deadline)
            if _webfig_responding(address, deadline):
                _action_remaining(deadline)
                del pending[node]
        if pending:
            time.sleep(min(2, _action_remaining(deadline)))


class ContainerlabEngine(LabEngine):
    def __init__(
        self,
        workdir: str,
        *,
        execution_policy: LabExecutionPolicy,
        lab_host_role: str | None = None,
        lock_label: str = "containerlab-engine",
    ):
        if not isinstance(execution_policy, LabExecutionPolicy):
            raise TypeError("an immutable lab execution policy is required")
        self._execution_policy = execution_policy
        self.workdir = workdir
        self.lab_host_role = lab_host_role if lab_host_role is not None else settings.lab_host_role
        # Identifies this engine's caller (worker vs. reconciler) in the host
        # lock file / contention messages — see app.services.host_lock.
        self.lock_label = lock_label

    @property
    def execution_policy(self) -> LabExecutionPolicy:
        return self._execution_policy

    def _require_lab_host(self) -> None:
        if self.lab_host_role.lower() != "lab":
            from app.metrics import LAB_WRONG_HOST_REFUSALS

            LAB_WRONG_HOST_REFUSALS.inc()
            raise WrongLabHostError(f"containerlab operations require LAB_HOST_ROLE=lab (got {self.lab_host_role!r})")

    def _topo_path(self, instance_name: str) -> str:
        return os.path.join(self.workdir, instance_name, "topo.clab.yml")

    def _inspect_all(self, *, deadline: float | None = None) -> object:
        result = _run_contained(
            [*_CLAB, "inspect", "--all", "--format", "json"],
            timeout=(
                self.execution_policy.lab_command_timeout_seconds_by_action["inspect"]
                if deadline is None
                else _action_remaining(deadline)
            ),
            grace_seconds=self.execution_policy.lab_process_termination_grace_seconds,
        )
        if result.returncode != 0:
            raise RuntimeError(f"inspect failed: {result.stderr}")
        try:
            return json.loads(result.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError("inspect returned invalid JSON") from exc

    def _inspect_lab_paths_unlocked(self) -> dict[str, set[str]]:
        """Every containerlab-reported lab name -> the set of ALL distinct
        real topology paths reported for it (never just the last one seen —
        see the module's Finding-3-fix note in ``_inventory_unlocked``/
        ``_destroy_unlocked`` for why a single mutable "last path" variable
        let an unowned same-named collision silently defeat an ownership
        check that only compared the final value).

        Unfiltered by design: a caller that already knows (by row identity)
        which specific instance it means to act on — e.g. ``_destroy_unlocked``
        below, recovering the real path for a lab whose expected topology
        file went missing — needs whatever path(s) containerlab actually
        reports, not an ownership judgement about it. :meth:`inventory` is
        the ownership-filtered view built on top of this for callers (like
        ``reconcile_runtime``) that must decide *which* names are Academy's
        to manage in the first place.

        Unlocked: callers must already hold ``host_lock``. Never call this
        (or any other ``_*_unlocked`` helper) from a public method other than
        the one already holding the lock — public methods must not call each
        other, since a second ``host_lock`` acquisition within the same
        process on a fresh fd does not merge with the first and instead
        fails as if a different process held it.
        """
        self._require_lab_host()
        raw = self._inspect_all()
        found: dict[str, set[str]] = {}

        def _walk(value: object, hinted_name: str | None = None) -> None:
            if isinstance(value, dict):
                lab_name = value.get("lab_name")
                if not isinstance(lab_name, str) or not lab_name:
                    lab_name = hinted_name
                if isinstance(lab_name, str) and lab_name:
                    group = found.setdefault(lab_name, set())
                    path = value.get("absLabPath") or value.get("labPath")
                    if isinstance(path, str) and path:
                        group.add(path)
                for key, child in value.items():
                    child_hint = key if isinstance(child, list) else lab_name
                    _walk(child, child_hint)
            elif isinstance(value, list):
                for child in value:
                    _walk(child, hinted_name)

        _walk(raw)
        return found

    def _inventory_unlocked(self) -> dict[str, str]:
        """Inspect every containerlab runtime, keeping only labs this engine owns.

        A ``dal-``-prefixed name alone is not proof of ownership — an unrelated
        containerlab lab on the same host (an operator's own test, or plain
        coincidence) could share that naming convention. The real topology
        path ``containerlab inspect`` reports is the actual owned-ness check:
        only a lab whose path matches this engine's own workdir convention
        (``self._topo_path(name)``) is retained. A mismatched or missing path
        is simply excluded, never raised on — containerlab's JSON shape can
        carry partial path info for labs this engine has no reason to trust
        anyway. This is the entrypoint used to decide *which* runtime names
        are eligible for orphan cleanup (``reconcile_runtime``); it is
        deliberately narrower than :meth:`_inspect_lab_paths_unlocked`. A name
        is retained only if this engine's own expected path is ONE OF the
        paths reported for it — never based on comparing against a single
        arbitrary "last seen" path, which a same-named unowned collision
        could otherwise control by sheer walk order.

        Unlocked — see :meth:`_inspect_lab_paths_unlocked`.
        """
        found = self._inspect_lab_paths_unlocked()
        result: dict[str, str] = {}
        for name, paths in found.items():
            expected = self._topo_path(name)
            if expected in paths:
                result[name] = expected
        return result

    def inventory(self) -> dict[str, str]:
        self._require_lab_host()
        with host_lock(self.lock_label, directory=self.workdir):
            return self._inventory_unlocked()

    def _lab_is_deployed_unlocked(self, instance_name: str) -> bool:
        """Inspect runtime state without trusting the expected topology file.

        Unlocked — see :meth:`_inspect_lab_paths_unlocked`.
        """
        return instance_name in self._inventory_unlocked()

    def _deploy_unlocked(self, topology_text: str, instance_name: str) -> LabHandle:
        """Unlocked — see :meth:`_inspect_lab_paths_unlocked`.

        The KVM fail-closed check lives here, not only in the public
        ``deploy()`` wrapper, so every caller of this helper — including
        ``reset()``, which calls it directly to avoid a nested lock
        acquisition — gets it automatically rather than relying on each
        caller to have remembered its own copy.
        """
        if _requires_kvm(topology_text) and not os.path.exists("/dev/kvm"):
            raise RuntimeError("vr-* lab deployment requires /dev/kvm")
        path = self._topo_path(instance_name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(topology_text)
        deadline = time.monotonic() + self.execution_policy.lab_command_timeout_seconds_by_action["deploy"]
        r = _run_contained(
            [*_CLAB, "deploy", "-t", path, "--format", "json"],
            timeout=_action_remaining(deadline),
            grace_seconds=self.execution_policy.lab_process_termination_grace_seconds,
        )
        if r.returncode != 0:
            raise RuntimeError(f"deploy failed: {r.stderr}")
        nodes, mgmt, kinds = {}, {}, {}
        prefix = f"clab-{instance_name}-"  # containerlab names: clab-<labname>-<node>
        data = json.loads(r.stdout)
        # `containerlab deploy --format json` → {"<labname>": [ {node...} ]}.
        # Be tolerant of a bare list too (older format / unit-test fixtures).
        if isinstance(data, dict):
            items: list = data.get(instance_name) or next(iter(data.values()), [])
        else:
            items = data
        for item in items:
            cname = item["name"]
            # strip the known prefix so dashed node names (e.g. client-a) survive
            logical = cname[len(prefix) :] if cname.startswith(prefix) else cname.split("-")[-1]
            nodes[logical] = cname
            mgmt[logical] = (item.get("ipv4_address") or item.get("ipv6_address") or "").split("/")[0]
            kinds[logical] = item.get("kind", "linux")
        handle = LabHandle(instance_name=instance_name, nodes=nodes, mgmt=mgmt, kinds=kinds)
        _wait_for_routeros(handle, deadline)
        return handle

    def deploy(self, topology_text: str, instance_name: str) -> LabHandle:
        self._require_lab_host()
        # The KVM check itself lives in _deploy_unlocked (see its docstring)
        # so reset() gets it too without a second, driftable copy here.
        with host_lock(self.lock_label, directory=self.workdir):
            return self._deploy_unlocked(topology_text, instance_name)

    def _ssh_exec_unlocked(
        self,
        handle: LabHandle,
        node: str,
        command: str,
        user: str,
        password: str,
    ) -> ExecResult:
        """Unlocked — see :meth:`_inspect_lab_paths_unlocked`."""
        ip = handle.mgmt[node]
        ssh = [
            "sshpass",
            "-p",
            password,
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            f"{user}@{ip}",
            command,
        ]
        r = _run_contained(
            ssh,
            timeout=self.execution_policy.lab_command_timeout_seconds_by_action["check"],
            grace_seconds=self.execution_policy.lab_process_termination_grace_seconds,
        )
        return ExecResult(stdout=r.stdout, stderr=r.stderr, exit_code=r.returncode)

    def ssh_exec(
        self,
        handle: LabHandle,
        node: str,
        command: str,
        user: str = "admin",
        password: str = "",
    ) -> ExecResult:
        self._require_lab_host()
        with host_lock(self.lock_label, directory=self.workdir):
            return self._ssh_exec_unlocked(handle, node, command, user, password)

    def _destroy_unlocked(self, instance_name: str) -> None:
        """Unlocked — see :meth:`_inspect_lab_paths_unlocked`."""
        path = self._topo_path(instance_name)
        # First deploys and replay after an already-completed destroy may have
        # no topology file. Confirm runtime absence rather than equating a lost
        # file with a destroyed lab: otherwise a live lab can leak silently.
        if not os.path.exists(path):
            # Unfiltered on purpose: the caller already identified this exact
            # instance_name (by row identity) as the one to destroy, so an
            # ownership judgement about the path is not needed here — only
            # reconcile_runtime's "which orphans are ours" decision needs the
            # ownership-filtered inventory(). But an exact-name collision is
            # still (however unlikely, given the name embeds tenant/person/
            # activity UUID fragments) not ruled out by name alone, so the
            # discovered path must match the one this engine would itself
            # have written before it's trusted — anything else is refused
            # rather than silently destroyed at an unverified location.
            discovered_paths = self._inspect_lab_paths_unlocked().get(instance_name)
            if discovered_paths is None:
                return
            if path not in discovered_paths:
                raise RuntimeError(
                    f"destroy refused: deployed lab {instance_name!r} was inspected "
                    f"at {sorted(discovered_paths)!r}, not the expected {path!r}"
                )
        r = _run_contained(
            [*_CLAB, "destroy", "-t", path, "--cleanup"],
            timeout=self.execution_policy.lab_command_timeout_seconds_by_action["destroy"],
            grace_seconds=self.execution_policy.lab_process_termination_grace_seconds,
        )
        if r.returncode != 0:
            raise RuntimeError(f"destroy failed: {r.stderr}")

    def destroy(self, instance_name: str) -> None:
        self._require_lab_host()
        with host_lock(self.lock_label, directory=self.workdir):
            self._destroy_unlocked(instance_name)

    def deploy_if_absent(self, topology_text: str, instance_name: str) -> LabHandle | None:
        """Observe-then-deploy inside exactly one ``host_lock`` acquisition.

        Deliberately does NOT compose the public :meth:`inventory`/
        :meth:`deploy` — each already acquires and releases its own
        ``host_lock`` independently, which would reopen a window between
        the observation and the mutation for another process to deploy the
        same instance in between. Instead this calls the already-existing
        unlocked helpers (``_lab_is_deployed_unlocked``/``_deploy_unlocked``)
        directly, within a single ``with host_lock(...):`` block: observe
        first, and only if genuinely absent, mutate — all before the lock is
        released. No mutation helper is called at all if the instance is
        already present.
        """
        self._require_lab_host()
        with host_lock(self.lock_label, directory=self.workdir):
            if self._lab_is_deployed_unlocked(instance_name):
                return None
            return self._deploy_unlocked(topology_text, instance_name)

    def destroy_if_present(self, instance_name: str) -> bool:
        """Observe-then-destroy inside exactly one ``host_lock`` acquisition.

        Same single-acquisition reasoning as :meth:`deploy_if_absent`.
        """
        self._require_lab_host()
        with host_lock(self.lock_label, directory=self.workdir):
            if not self._lab_is_deployed_unlocked(instance_name):
                return False
            self._destroy_unlocked(instance_name)
            return True

    def reset(self, topology_text: str, instance_name: str) -> LabHandle:
        self._require_lab_host()
        # Held for the whole destroy+deploy pair so no other host operation
        # can interleave between them — calling the public destroy()/deploy()
        # in sequence would each acquire and fully release the lock, leaving
        # a window between the two where another process could run.
        with host_lock(self.lock_label, directory=self.workdir):
            self._destroy_unlocked(instance_name)
            return self._deploy_unlocked(topology_text, instance_name)

    def _exec_unlocked(self, handle: LabHandle, node: str, command: list) -> ExecResult:
        """Unlocked — see :meth:`_inspect_lab_paths_unlocked`."""
        cname = handle.nodes[node]
        r = _run_contained(
            ["docker", "exec", cname, *command],
            timeout=self.execution_policy.lab_command_timeout_seconds_by_action["check"],
            grace_seconds=self.execution_policy.lab_process_termination_grace_seconds,
        )
        return ExecResult(stdout=r.stdout, stderr=r.stderr, exit_code=r.returncode)

    def exec(self, handle: LabHandle, node: str, command: list) -> ExecResult:
        self._require_lab_host()
        with host_lock(self.lock_label, directory=self.workdir):
            return self._exec_unlocked(handle, node, command)

    def status(self, instance_name: str) -> str:
        self._require_lab_host()
        with host_lock(self.lock_label, directory=self.workdir):
            return "running" if self._lab_is_deployed_unlocked(instance_name) else "absent"

    def _inspect_running_handle_unlocked(self, instance_name: str, *, deadline: float) -> LabHandle | None:
        """Reconstruct a live :class:`LabHandle` for ``instance_name`` from
        ``containerlab inspect --all``'s own per-node fields, never a
        redeploy. Returns ``None`` if not currently running. An owned RouterOS
        handle must also answer WebFig within the shared inspect action deadline.

        Mirrors :meth:`_destroy_unlocked`'s own ownership-verification
        pattern exactly: a ``lab_name`` match alone is not proof of
        ownership — an exact-name collision from OUTSIDE Academy's own
        workdir is still (however unlikely, given the name embeds tenant/
        person/activity UUID fragments) not ruled out by name alone, so the
        discovered path must match ``self._topo_path(instance_name)`` (the
        path this engine would itself have written) before any of its node
        data is trusted. Unlike ``_destroy_unlocked`` (which raises on a
        mismatch, since destroying at an unverified location would be
        actively destructive), a mismatch or missing path here just returns
        ``None`` — this is a read-only resync helper, so "we couldn't safely
        confirm ownership" is treated exactly like "not found/not running":
        the caller (``_rebuild_consoles_from_live_inspection``) already
        handles a ``None`` result by setting ``runtime_presence="unknown"``
        and leaving ``status``/``error`` alone.

        Node data is grouped by the path it was reported under DURING the
        walk (never accumulated into flat dicts as records are visited), so
        a same-named collision reported at a different, unowned path can
        never contribute even a single node to the accepted result: only
        the one group whose path exactly matches ``self._topo_path(
        instance_name)`` is ever read from, after the walk completes. A
        single flat accumulator keyed only by logical node name — with a
        separate "last path seen" variable checked once at the end — would
        let an untrusted record's nodes survive being overwritten by a
        later, legitimately-pathed record's path check while still keeping
        entries an earlier, unverified record contributed; grouping by path
        closes that gap by construction.

        Assumes ``inspect --all --format json`` reports the same per-node
        ``name``/``ipv4_address``/``kind`` fields :meth:`_deploy_unlocked`
        already relies on from ``deploy``'s own JSON output — both are the
        same underlying containerlab container inventory, so this is a
        reasonable assumption, but (like the sudo/session-propagation
        caveat already documented on :func:`_terminate_process_group`) it
        cannot be fully proven from this repository alone and should be
        spot-checked against the real lab host as part of rollout.

        Unlocked — see :meth:`_inspect_lab_paths_unlocked`.
        """
        raw = self._inspect_all(deadline=deadline)
        prefix = f"clab-{instance_name}-"
        # path -> {logical_name: (cname, ip, kind)}. A record with no
        # discoverable path groups under the ``None`` key, which can never
        # equal ``self._topo_path(instance_name)`` (that method always
        # returns a non-empty string), so such records are structurally
        # excluded from ever being accepted.
        groups: dict[str | None, dict[str, tuple[str, str, str]]] = {}

        def _walk(value: object, hinted_name: str | None = None) -> None:
            if isinstance(value, dict):
                lab_name = value.get("lab_name")
                if not isinstance(lab_name, str) or not lab_name:
                    lab_name = hinted_name
                if lab_name == instance_name:
                    path = value.get("absLabPath") or value.get("labPath")
                    group_key = path if isinstance(path, str) and path else None
                    cname = value.get("name")
                    if isinstance(cname, str) and cname:
                        logical = cname[len(prefix) :] if cname.startswith(prefix) else cname.split("-")[-1]
                        ip = (value.get("ipv4_address") or value.get("ipv6_address") or "").split("/")[0]
                        kind = value.get("kind", "linux")
                        group = groups.setdefault(group_key, {})
                        group[logical] = (cname, ip, kind)
                for key, child in value.items():
                    child_hint = key if isinstance(child, list) else lab_name
                    _walk(child, child_hint)
            elif isinstance(value, list):
                for child in value:
                    _walk(child, hinted_name)

        _walk(raw)
        expected_path = self._topo_path(instance_name)
        group = groups.get(expected_path)
        if not group:
            return None
        nodes = {logical: cname for logical, (cname, _ip, _kind) in group.items()}
        mgmt = {logical: ip for logical, (_cname, ip, _kind) in group.items()}
        kinds = {logical: kind for logical, (_cname, _ip, kind) in group.items()}
        handle = LabHandle(instance_name=instance_name, nodes=nodes, mgmt=mgmt, kinds=kinds)
        _wait_for_routeros(handle, deadline)
        return handle

    def inspect_running(self, instance_name: str) -> LabHandle | None:
        self._require_lab_host()
        with host_lock(self.lock_label, directory=self.workdir):
            deadline = time.monotonic() + self.execution_policy.lab_command_timeout_seconds_by_action["inspect"]
            return self._inspect_running_handle_unlocked(instance_name, deadline=deadline)

    def console_target(self, handle: LabHandle, node: str) -> str:
        return handle.nodes[node]
