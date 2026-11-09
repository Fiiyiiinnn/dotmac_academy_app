"""Historical host-lock unit cases, isolated from deferred runtime adapters.

Original function bodies/assertions are retained; source only, NOT EXECUTED.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import time
from unittest.mock import patch

import pytest

from app.services import host_lock as host_lock_module
from app.services.host_lock import HostLockUnavailable, worker_singleton_lock


def _hold_manually(path: str) -> int:
    """Open+flock ``path`` on a fresh fd, simulating an external holder.

    Returns the fd; the caller is responsible for unlocking and closing it.
    Deliberately bypasses :func:`host_lock` so this fd is a genuinely separate
    open file description from anything the module under test opens — the
    same distinction that makes two processes (or, as here, two independent
    fds in one process) contend on a real ``flock``.
    """
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def test_second_acquisition_raises_host_lock_unavailable(tmp_path):
    path = host_lock_module.lock_path(directory=str(tmp_path))
    holder_fd = _hold_manually(path)
    os.write(holder_fd, f"{os.getpid()} academy-lab-worker\n".encode())
    try:
        with pytest.raises(HostLockUnavailable):
            with host_lock_module.host_lock("academy-lab-reconcile", directory=str(tmp_path)):
                pass
    finally:
        fcntl.flock(holder_fd, fcntl.LOCK_UN)
        os.close(holder_fd)


def test_contention_message_names_the_first_holder(tmp_path):
    path = host_lock_module.lock_path(directory=str(tmp_path))
    holder_fd = _hold_manually(path)
    holder_pid = os.getpid()
    os.write(holder_fd, f"{holder_pid} academy-lab-worker\n".encode())
    try:
        with pytest.raises(HostLockUnavailable) as exc_info:
            with host_lock_module.host_lock("academy-lab-reconcile", directory=str(tmp_path)):
                pass
    finally:
        fcntl.flock(holder_fd, fcntl.LOCK_UN)
        os.close(holder_fd)

    message = str(exc_info.value)
    assert str(holder_pid) in message
    assert "academy-lab-worker" in message


def test_lock_is_released_when_the_holding_process_is_killed(tmp_path):
    """A killed holder's fcntl lock is released by the kernel, not by cleanup code."""
    path = host_lock_module.lock_path(directory=str(tmp_path))
    script = (
        "import fcntl, os, time\n"
        f"fd = os.open({str(path)!r}, os.O_RDWR | os.O_CREAT, 0o644)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX)\n"
        "os.write(fd, b'held by crash-test child\\n')\n"
        "time.sleep(60)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", script])  # noqa: S603
    try:
        deadline = time.monotonic() + 10
        contended = False
        while time.monotonic() < deadline:
            try:
                with host_lock_module.host_lock("probe", directory=str(tmp_path)):
                    pass
            except HostLockUnavailable:
                contended = True
                break
            time.sleep(0.05)
        assert contended, "child process never actually took the lock"

        proc.kill()
        proc.wait(timeout=10)

        deadline = time.monotonic() + 10
        acquired = False
        while time.monotonic() < deadline:
            try:
                with host_lock_module.host_lock("after-crash", directory=str(tmp_path)):
                    acquired = True
                break
            except HostLockUnavailable:
                time.sleep(0.05)
        assert acquired, "lock was not released after its holder was killed"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


@pytest.mark.parametrize("faulting_call", ["ftruncate", "write", "fsync"])
def test_metadata_write_failure_releases_the_flock_and_closes_the_fd(
    tmp_path, monkeypatch, faulting_call
):
    """A failure writing the holder metadata (ENOSPC/EIO/quota-shaped) after
    the flock already succeeded must not leak the fd or its flock. The
    original exception must still propagate, and a fresh acquisition attempt
    on the same path must succeed afterwards — which is only possible if the
    flock was actually released and the fd actually closed.
    """
    real_fn = getattr(os, faulting_call)
    call_count = 0

    def _faulting(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        raise OSError("simulated fault")

    monkeypatch.setattr(os, faulting_call, _faulting)

    with pytest.raises(OSError, match="simulated fault"):
        with host_lock_module.host_lock("academy-lab-worker", directory=str(tmp_path)):
            pass
    assert call_count == 1

    # Restore the real syscall so the probing acquisition below actually
    # writes its metadata instead of faulting again.
    monkeypatch.setattr(os, faulting_call, real_fn)

    # If the flock/fd had leaked, this would raise HostLockUnavailable.
    with host_lock_module.host_lock("probe-after-fault", directory=str(tmp_path)):
        pass


def test_contention_message_names_the_holder_at_the_moment_of_contention(tmp_path):
    """The reported holder must reflect who ACTUALLY holds the lock at the
    moment the flock attempt fails, not whoever the recorded holder was
    slightly earlier. The recorded holder is mutated as a side effect of the
    real (failing) ``fcntl.flock`` call itself, so an implementation that
    reads ``_holder_description`` any earlier than its own ``OSError``
    handling — e.g. eagerly, while building the message before even
    attempting the flock — would still report the stale value; only reading
    it lazily, inside the failure handling, sees the mutated one.
    """
    path = host_lock_module.lock_path(directory=str(tmp_path))
    holder_fd = _hold_manually(path)
    os.write(holder_fd, f"{os.getpid()} stale-holder\n".encode())
    real_flock = fcntl.flock

    def _flock_then_mutate_recorded_holder(fd, operation):
        try:
            return real_flock(fd, operation)
        except OSError:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(f"{os.getpid()} current-holder\n")
            raise

    try:
        with patch("fcntl.flock", side_effect=_flock_then_mutate_recorded_holder):
            with pytest.raises(HostLockUnavailable) as exc_info:
                with host_lock_module.host_lock(
                    "academy-lab-reconcile", directory=str(tmp_path)
                ):
                    pass
    finally:
        fcntl.flock(holder_fd, fcntl.LOCK_UN)
        os.close(holder_fd)

    message = str(exc_info.value)
    assert "current-holder" in message
    assert "stale-holder" not in message


def test_worker_lock_path_and_operation_lock_path_are_different_files(tmp_path):
    """The lifetime worker lock and the per-operation containerlab lock must
    never collide on the same file — one is held for a whole process's
    lifetime, the other per containerlab call.
    """
    op_path = host_lock_module.lock_path(directory=str(tmp_path))
    worker_path = host_lock_module.worker_lock_path(directory=str(tmp_path))
    assert op_path != worker_path


def test_second_worker_singleton_lock_acquisition_raises(tmp_path):
    path = host_lock_module.worker_lock_path(directory=str(tmp_path))
    holder_fd = _hold_manually(path)
    os.write(holder_fd, f"{os.getpid()} academy-lab-worker\n".encode())
    try:
        with pytest.raises(HostLockUnavailable):
            with worker_singleton_lock(directory=str(tmp_path)):
                pass
    finally:
        fcntl.flock(holder_fd, fcntl.LOCK_UN)
        os.close(holder_fd)


def test_worker_singleton_lock_is_released_when_the_holding_process_is_killed(tmp_path):
    """Same crash-release property as the per-operation lock — proven
    separately here since this is a distinct lock file/context manager.
    """
    path = host_lock_module.worker_lock_path(directory=str(tmp_path))
    script = (
        "import fcntl, os, time\n"
        f"fd = os.open({str(path)!r}, os.O_RDWR | os.O_CREAT, 0o644)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX)\n"
        "os.write(fd, b'held by crash-test child\\n')\n"
        "time.sleep(60)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", script])  # noqa: S603
    try:
        deadline = time.monotonic() + 10
        contended = False
        while time.monotonic() < deadline:
            try:
                with worker_singleton_lock(directory=str(tmp_path)):
                    pass
            except HostLockUnavailable:
                contended = True
                break
            time.sleep(0.05)
        assert contended, "child process never actually took the worker lock"

        proc.kill()
        proc.wait(timeout=10)

        deadline = time.monotonic() + 10
        acquired = False
        while time.monotonic() < deadline:
            try:
                with worker_singleton_lock(directory=str(tmp_path)):
                    acquired = True
                break
            except HostLockUnavailable:
                time.sleep(0.05)
        assert acquired, "worker lock was not released after its holder was killed"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
