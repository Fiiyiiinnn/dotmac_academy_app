"""Host-wide containerlab lock: contention, holder naming, and crash-release.

No live database is needed anywhere in this file — the lock is pure
filesystem/fcntl behavior, and the one ``reconcile_runtime``/``_lab_reconcile``
test doubles the database session entirely because both functions are proven
here to make no database call before a host-lock contention can be raised.

Worker-side deferral/refund behavior on ``HostLockUnavailable`` (Stream A's
``run_claimed`` contract) belongs in that stream's own test file and is
deliberately not duplicated here.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from app.config import LabExecutionPolicy, settings
from app.services import host_lock as host_lock_module
from app.services.host_lock import HostLockUnavailable, worker_singleton_lock
from app.services.labengine.containerlab import ContainerlabEngine


def _policy() -> LabExecutionPolicy:
    """The product's env-derived execution policy (what the worker builds per pass)."""
    return LabExecutionPolicy.from_settings(settings)


def _fake_popen(returncode: int = 0, stdout: str = "", stderr: str = ""):
    proc = MagicMock()
    proc.pid = 4321
    proc.communicate.return_value = (stdout, stderr)
    proc.returncode = returncode
    return proc


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


def test_worker_and_reconciler_engines_contend_on_the_same_lock_file(tmp_path):
    """Different labels must not mean different locks — they share one file."""
    policy = _policy()
    worker_engine = ContainerlabEngine(
        str(tmp_path), lab_host_role="lab", execution_policy=policy, lock_label="academy-lab-worker"
    )
    reconciler_engine = ContainerlabEngine(
        str(tmp_path), lab_host_role="lab", execution_policy=policy, lock_label="academy-lab-reconcile"
    )
    path = host_lock_module.lock_path(directory=str(tmp_path))
    holder_fd = _hold_manually(path)
    try:
        with patch("subprocess.Popen") as popen:
            with pytest.raises(HostLockUnavailable):
                worker_engine.status("i")
            with pytest.raises(HostLockUnavailable):
                reconciler_engine.status("i")
            popen.assert_not_called()
    finally:
        fcntl.flock(holder_fd, fcntl.LOCK_UN)
        os.close(holder_fd)

    # Once released, both engines succeed against the same file — proving the
    # contention above was one shared lock, not one lock each that happened
    # to both be unheld.
    with patch("subprocess.Popen") as popen:
        popen.return_value = _fake_popen(stdout="{}")
        assert worker_engine.status("i") == "absent"
        assert reconciler_engine.status("i") == "absent"


def test_contended_deploy_raises_without_starting_a_subprocess(tmp_path):
    eng = ContainerlabEngine(str(tmp_path), lab_host_role="lab", execution_policy=_policy())
    path = host_lock_module.lock_path(directory=str(tmp_path))
    holder_fd = _hold_manually(path)
    try:
        with patch("subprocess.Popen") as popen:
            with pytest.raises(HostLockUnavailable):
                eng.deploy("name: x", "i")
            popen.assert_not_called()
    finally:
        fcntl.flock(holder_fd, fcntl.LOCK_UN)
        os.close(holder_fd)
    assert not (tmp_path / "i").exists()


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


def test_reconcile_runtime_propagates_contention_before_touching_the_database():
    """Proves the phase ordering: the locked inventory call happens first,
    before any database read — so a contention here needs nothing rolled
    back, and reconcile_runtime does not swallow it into a fabricated
    successful-looking result.
    """
    from app.services import lab_jobs

    policy = _policy()
    engine = MagicMock()
    engine.execution_policy = policy
    engine.inventory.side_effect = HostLockUnavailable(
        "containerlab host lock is already held — held by '999 academy-lab-worker'"
    )
    db = MagicMock()

    with pytest.raises(HostLockUnavailable):
        lab_jobs.reconcile_runtime(db, engine, policy=policy)

    db.scalars.assert_not_called()


def test_lab_reconcile_skips_cleanly_on_host_lock_contention(monkeypatch, capsys):
    """The CLI command itself must not raise or partially commit on contention.

    reconcile_runtime propagates HostLockUnavailable (proven above);
    _lab_reconcile is the layer that must catch it, roll back, skip the
    console sweep, and return normally so the process exits 0 and the
    1-minute timer simply retries.
    """
    from app import cli
    from app.config import settings
    from app.services import lab_jobs, lab_operations

    monkeypatch.setattr(settings, "lab_host_role", "lab")
    monkeypatch.setattr("app.config.validate_settings", lambda s: [])
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/containerlab")

    fake_db = MagicMock()

    @contextmanager
    def fake_session():
        yield fake_db

    monkeypatch.setattr(lab_jobs, "lab_worker_session", fake_session)
    monkeypatch.setattr(lab_operations, "reconcile_stuck", lambda db, *, policy: 0)

    def _raise_contention(db, engine, *, policy):
        raise HostLockUnavailable(
            "containerlab host lock is already held — held by '123 academy-lab-worker'"
        )

    monkeypatch.setattr(lab_jobs, "reconcile_runtime", _raise_contention)
    sweep = MagicMock()
    monkeypatch.setattr(lab_jobs, "sweep_orphan_consoles", sweep)

    cli._lab_reconcile(argparse.Namespace())

    sweep.assert_not_called()
    fake_db.rollback.assert_called_once()
    # Only reconcile_stuck's own short-transaction commit happened; nothing
    # from the abandoned reconcile_runtime pass was committed afterwards.
    assert fake_db.commit.call_count == 1
    assert "skipped" in capsys.readouterr().out
