"""Fail-closed SMTP admission under a durable, root-controlled sender hold.

Provisioning/operators own the directory, stable lock inode and atomic state
replacement. Senders never create or modify these files. A holder first replaces
state with ``HELD\n``, then drains this lock exclusively; releasing that lock does
not reopen delivery. Only an explicit atomic replacement with ``OPEN\n`` does.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

GATE_DIRECTORY = Path("/var/lib/dotmac-academy/mail-delivery")
LOCK_PATH = GATE_DIRECTORY / "lock"
STATE_PATH = GATE_DIRECTORY / "state"


class EmailDeliveryDeferred(Exception):
    """Delivery was refused before an SMTP attempt; retry without charging it."""


@dataclass
class _Lease:
    fd: int
    directory_identity: tuple[int, int]
    lock_identity: tuple[int, int]
    pid: int
    thread_id: int
    task: object | None
    active: bool = True


_current_lease: ContextVar[_Lease | None] = ContextVar("email_delivery_lease", default=None)
_open_fds: set[int] = set()
_fd_guard = threading.Lock()


def _task() -> object | None:
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _safe_directory(info: os.stat_result, *, final: bool = False) -> bool:
    mode = stat.S_IMODE(info.st_mode)
    return stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not mode & 0o022 and (not final or mode == 0o755)


def _safe_file(info: os.stat_result) -> bool:
    return (
        stat.S_ISREG(info.st_mode) and info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o644 and info.st_nlink == 1
    )


def _open_directory() -> int:
    if (
        not GATE_DIRECTORY.is_absolute()
        or ".." in GATE_DIRECTORY.parts
        or LOCK_PATH != GATE_DIRECTORY / "lock"
        or STATE_PATH != GATE_DIRECTORY / "state"
    ):
        raise EmailDeliveryDeferred
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open("/", flags)
    try:
        if not _safe_directory(os.fstat(fd)):
            raise EmailDeliveryDeferred
        components = GATE_DIRECTORY.parts[1:]
        for index, component in enumerate(components):
            next_fd = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            if not _safe_directory(os.fstat(fd), final=index == len(components) - 1):
                raise EmailDeliveryDeferred
        return fd
    except BaseException:
        os.close(fd)
        raise


def _validate_lock(lease: _Lease, directory_fd: int) -> None:
    opened = os.fstat(lease.fd)
    named = os.stat(LOCK_PATH.name, dir_fd=directory_fd, follow_symlinks=False)
    if (
        _identity(os.fstat(directory_fd)) != lease.directory_identity
        or not _safe_file(opened)
        or not _safe_file(named)
        or _identity(opened) != lease.lock_identity
        or _identity(named) != lease.lock_identity
    ):
        raise EmailDeliveryDeferred


def _validate_nested(lease: _Lease) -> None:
    directory_fd = _open_directory()
    try:
        _validate_lock(lease, directory_fd)
    finally:
        os.close(directory_fd)


def _after_fork() -> None:
    # flock's open-file description is inherited. LOCK_UN here would unlock the
    # parent's lease too, so close only the child's descriptors.
    try:
        for fd in _open_fds:
            os.close(fd)
        _open_fds.clear()
        _current_lease.set(None)
    finally:
        _fd_guard.release()


os.register_at_fork(before=_fd_guard.acquire, after_in_parent=_fd_guard.release, after_in_child=_after_fork)


def _close_lock(fd: int) -> None:
    with _fd_guard:
        _open_fds.discard(fd)
        os.close(fd)


@contextmanager
def delivery_lease() -> Iterator[_Lease]:
    """Hold shared SMTP admission through delivery and its attempt bookkeeping.

    Nested calls in the owning process/thread/task reuse the admitted lease,
    including after an operator publishes HELD while waiting for it to drain.
    Copied contexts in other threads/tasks and inherited fork contexts cannot
    borrow that admission. All unavailable/unsafe gate states defer uniformly.
    """
    existing = _current_lease.get()
    if existing is not None:
        if (
            not existing.active
            or existing.pid != os.getpid()
            or existing.thread_id != threading.get_ident()
            or existing.task is not _task()
        ):
            raise EmailDeliveryDeferred
        try:
            _validate_nested(existing)
        except (OSError, ValueError) as exc:
            raise EmailDeliveryDeferred from exc
        yield existing
        return

    fd: int | None = None
    directory_fd: int | None = None
    try:
        directory_fd = _open_directory()
        with _fd_guard:
            fd = os.open(
                LOCK_PATH.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=directory_fd
            )
            _open_fds.add(fd)
        lease = _Lease(
            fd=fd,
            directory_identity=_identity(os.fstat(directory_fd)),
            lock_identity=_identity(os.fstat(fd)),
            pid=os.getpid(),
            thread_id=threading.get_ident(),
            task=_task(),
        )
        if not _safe_file(os.fstat(fd)):
            raise EmailDeliveryDeferred
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        _validate_lock(lease, directory_fd)
        state_fd = os.open(
            STATE_PATH.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=directory_fd
        )
        try:
            if not _safe_file(os.fstat(state_fd)) or os.read(state_fd, 6) != b"OPEN\n":
                raise EmailDeliveryDeferred
        finally:
            os.close(state_fd)
        _validate_lock(lease, directory_fd)
    except (OSError, ValueError) as exc:
        if fd is not None:
            _close_lock(fd)
        raise EmailDeliveryDeferred from exc
    except BaseException:
        if fd is not None:
            _close_lock(fd)
        raise
    finally:
        if directory_fd is not None:
            os.close(directory_fd)

    token = _current_lease.set(lease)
    try:
        yield lease
    finally:
        lease.active = False
        _current_lease.reset(token)
        if lease.pid == os.getpid():
            _close_lock(lease.fd)
