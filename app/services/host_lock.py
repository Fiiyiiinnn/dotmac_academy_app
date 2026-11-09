"""Host-level mutual exclusion for containerlab operations.

``containerlab`` mutates shared host state — the docker bridge, iptables/
nftables rules, ``/etc/hosts`` — that is not namespaced per lab. Two of these
run against the same lab host today with no serialisation between them:
``_lab_worker`` (a continuous 5s-poll loop draining durable operations) and
``_lab_reconcile`` (a 1-minute timer that inspects/repairs runtime drift).
Concurrent ``sudo containerlab`` invocations on different labs can therefore
race on that shared state. This module is the fix: a single host-wide,
non-blocking, crash-safe advisory lock both callers take before touching
containerlab.

Design notes (parallel to `dotmac_starter_mt`'s deployment lock, which is
inspiration only — this module has no dependency on that package):

- **Non-blocking.** ``fcntl.flock(..., LOCK_EX | LOCK_NB)``. A worker or
  reconciler pass that BLOCKED waiting for the lock would stall its whole
  loop; refusing immediately and letting the caller retry on its own natural
  cadence (5s poll / 1min timer) is the safe answer.
- **The refusal names the holder.** The lock file records the holding PID and
  a caller-supplied label. Knowing the lock is held is useless without
  knowing which process holds it.
- **It survives a crash.** An advisory ``fcntl`` lock is released by the
  kernel the instant the holding process exits, however it exits — clean
  return, exception, SIGKILL. A lock implemented as "does this file exist"
  does not have this property, and requires an operator to notice and delete
  a stale lock file by hand.
- **The file is never deleted.** Unlinking it would open a window where one
  process has unlinked the path while another already opened the same inode,
  and both then believe they hold the lock — the classic lockfile race. An
  empty, never-truncated-away file costs nothing to keep around.
"""

from __future__ import annotations

import errno
import fcntl
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from app.config import settings

__all__ = [
    "HostLockUnavailable",
    "host_lock",
    "lock_path",
    "worker_lock_path",
    "worker_singleton_lock",
]


class HostLockUnavailable(RuntimeError):
    """Raised when the host containerlab lock is already held by another process."""


def _acquire_exclusive_nonblocking(path: str, label: str, *, describe_unavailable: Callable[[str], str]) -> int:
    """Open ``path``, take a non-blocking exclusive ``flock`` on it, and record the holder.

    Shared primitive behind both :func:`host_lock` (per-operation, released at
    the end of each containerlab call) and :func:`worker_singleton_lock`
    (held for a whole worker process's lifetime) — the acquire/record/failure
    behavior is identical between the two; only how long the caller holds the
    resulting fd differs. Raises :class:`HostLockUnavailable` (closing the fd
    first) if another process already holds the lock; otherwise returns the
    open, locked fd for the caller to hold and eventually unlock/close.

    ``describe_unavailable`` builds the caller-specific contention message
    from a holder description — it is invoked only if and when the flock
    attempt actually fails, INSIDE this function's own exception handling,
    never before the attempt. Evaluating ``_holder_description(path)`` any
    earlier (e.g. as part of building a message string before this call) can
    report a holder that has already changed by the time the flock attempt
    genuinely contends, which would contradict this module's documented
    guarantee that "the refusal names the [current] holder".

    Residual, accepted race (diagnostic-only, not a correctness gap): the
    winner's holder-metadata write (``ftruncate``/``write``/``fsync`` below)
    is not atomic with its ``flock()`` acquisition. A contender whose own
    ``flock()`` attempt fails in the narrow window between the winner's
    successful ``flock()`` and that metadata write completing will read
    whatever was previously in the file via :func:`_holder_description` —
    stale, from an earlier holder, or empty for a fresh file — instead of the
    current winner's identity. This affects only the precision of the
    human-facing contention message; actual mutual exclusion is governed
    entirely by the kernel's atomic ``flock()`` call and is unaffected.
    Deliberately left undocumented-but-unfixed further than this: closing it
    (e.g. write-then-atomic-rename, or a second lock guarding the metadata
    write) would be disproportionate machinery for a rare, low-severity,
    diagnostic-only edge case.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
            raise
        raise HostLockUnavailable(describe_unavailable(_holder_description(path))) from exc
    try:
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()} {label}\n".encode())
        os.fsync(fd)
    except OSError:
        # The flock already succeeded by this point — a failure writing the
        # holder metadata (e.g. ENOSPC/EIO/quota) must not leak the fd (and,
        # with it, the flock it holds) for the rest of this process's
        # lifetime. Release both before propagating the original exception
        # unchanged, mirroring the close-on-failure discipline in the
        # flock-acquisition branch just above.
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        raise
    return fd


def lock_path(directory: str | os.PathLike[str] | None = None) -> str:
    """Where the single host-wide containerlab lock file lives.

    Deliberately one path, not per-product/per-lab: the incident this guards
    against is two DIFFERENT labs' containerlab invocations mutating the same
    shared host state (bridge, iptables, /etc/hosts) concurrently, so every
    caller — worker and reconciler alike — must contend for the same lock.

    Defaults to ``settings.lab_workdir`` (default ``/home/dotmac/labs``)
    rather than ``/var/lock``: the worker/reconciler already have write
    access there, so this needs no new host permissions. ``ContainerlabEngine``
    passes its own configured ``workdir`` explicitly, which in production is
    always ``settings.lab_workdir`` (both ``_lab_worker`` and
    ``_lab_reconcile`` construct their engine with it) — the two stay
    identical in every real deployment, and the parameter only exists so
    tests can point an engine at an isolated ``tmp_path`` without a real
    lock file colliding with ``settings.lab_workdir`` on the machine
    running the tests.
    """
    base = directory if directory is not None else settings.lab_workdir
    return os.path.join(base, ".academy-containerlab.lock")


def _holder_description(path: str) -> str:
    """Whatever the lock file says about who holds it, without guessing.

    Best-effort only: see the residual-race note in
    :func:`_acquire_exclusive_nonblocking` — a contender reading this file in
    the narrow window between a winner's ``flock()`` and that winner's
    metadata write completing can see stale or empty content here rather
    than the current holder.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            recorded = fh.read().strip()
    except OSError:
        return "the holder could not be read"
    if not recorded:
        return "the holder is unrecorded"
    return f"held by {recorded!r}"


@contextmanager
def host_lock(label: str, *, directory: str | os.PathLike[str] | None = None) -> Iterator[None]:
    """Hold the exclusive host containerlab lock for the duration of the block.

    Non-blocking: raises :class:`HostLockUnavailable` immediately if another
    process already holds it, naming that holder's recorded PID/label rather
    than blocking the caller's poll loop.

    ``label`` identifies the caller (e.g. ``"academy-lab-worker"`` or
    ``"academy-lab-reconcile"``) so contention messages and the lock file
    itself say who is doing what. ``directory`` overrides where the lock
    file lives (see :func:`lock_path`); omit it to use
    ``settings.lab_workdir``.
    """
    path = lock_path(directory)
    fd = _acquire_exclusive_nonblocking(
        path,
        label,
        describe_unavailable=lambda holder: (
            f"containerlab host lock {path} is already held — "
            f"{holder}; refusing to run {label!r} "
            "concurrently with another containerlab operation on this host"
        ),
    )
    try:
        yield
    finally:
        # Released on normal exit AND on the holder crashing — a killed
        # process has this fd closed by the kernel, which drops the
        # fcntl lock automatically. No special crash handling needed.
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def worker_lock_path(directory: str | os.PathLike[str] | None = None) -> str:
    """Where the worker's whole-process-lifetime singleton lock file lives.

    Deliberately a DIFFERENT file from :func:`lock_path`'s per-operation
    containerlab lock: that lock is acquired and released once per
    containerlab call by both the continuous worker and the 1-minute
    reconciler, and must keep contending on a shared file between the two.
    This lock is acquired exactly once, at worker startup, and held for the
    whole process's lifetime — merging it onto the same file would make the
    reconciler (and every single containerlab call the worker itself makes)
    contend against the worker's own long-lived hold of it. Defaults to
    ``settings.lab_workdir`` for the same reason :func:`lock_path` does.
    """
    base = directory if directory is not None else settings.lab_workdir
    return os.path.join(base, ".academy-lab-worker.lock")


@contextmanager
def worker_singleton_lock(
    label: str = "academy-lab-worker",
    *,
    directory: str | os.PathLike[str] | None = None,
) -> Iterator[None]:
    """Hold an exclusive, whole-process-lifetime lock for the lab worker.

    Unlike :func:`host_lock` (acquired and released per containerlab call),
    this is acquired ONCE at worker startup — before the worker opens any
    database session or does any reclaim/drain work — and held for the
    worker's entire polling loop. Its purpose is to make it impossible for
    two worker processes (a manual invocation racing the systemd-managed
    worker, or two systemd instances somehow both starting) to run
    concurrently against the same lab host at all, which the per-operation
    ``host_lock`` alone does not guarantee between polling iterations.

    The reconciler (``_lab_reconcile``) must NOT acquire this lock — it
    continues to contend only on the shorter-lived, per-operation
    :func:`host_lock`, unchanged.

    Non-blocking, like :func:`host_lock`: raises :class:`HostLockUnavailable`
    immediately if another process already holds it, rather than blocking
    worker startup. Never unlinks the lock file (see :func:`host_lock`'s
    docstring for why: unlinking would open a stale-inode race).
    """
    path = worker_lock_path(directory)
    fd = _acquire_exclusive_nonblocking(
        path,
        label,
        describe_unavailable=lambda holder: (
            f"worker singleton lock {path} is already held — "
            f"{holder}; another {label!r} process appears "
            "to already be running on this host"
        ),
    )
    try:
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
