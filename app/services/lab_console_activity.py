"""Console-use heartbeat: typing in a lab console keeps the lab alive.

The web tier may not write ``lab_instances`` (worker-owned), so it records the
fact "this learner used a console" in ``lab_console_activity`` and the idle
reaper treats ``greatest(last_active_at, last_seen_at)`` as the last activity.

Rate limiting is two-layered and deliberately cheap:

* in-process: at most one attempt per instance per ``HEARTBEAT_INTERVAL_SECONDS``
  (monotonic clock, bounded dict), so keystrokes never reach the database;
* in SQL: the upsert only rewrites a row older than the interval, so several
  web workers together still write at most about once per interval per lab.

A heartbeat failure must never break a console; callers get ``False``.
"""

from __future__ import annotations

import logging
import time
from uuid import UUID

from sqlalchemy import func, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.lab import LabConsoleActivity

logger = logging.getLogger(__name__)

#: Fixed on purpose (no setting): lab time limits live in the settings work.
HEARTBEAT_INTERVAL_SECONDS = 60
_MAX_TRACKED = 4096

_last_attempt: dict[UUID, float] = {}

#: A failing heartbeat (for example a grant missing in production) must be
#: visible, but must not flood the log from every keystroke window.
_WARN_EVERY_SECONDS = 300.0
_last_warned: float | None = None


def warn_failure(instance_id: UUID, exc: BaseException) -> None:
    """Log a heartbeat failure at WARNING, at most once per ``_WARN_EVERY_SECONDS``."""
    global _last_warned
    now = time.monotonic()
    if _last_warned is not None and now - _last_warned < _WARN_EVERY_SECONDS:
        logger.debug("console heartbeat failed for %s: %s", instance_id, exc)
        return
    _last_warned = now
    logger.warning(
        "console heartbeat failed (labs will fall back to lifecycle activity only): %s: %s",
        type(exc).__name__,
        exc,
    )


def _due(instance_id: UUID, now: float) -> bool:
    """Claim this instance's heartbeat slot; False if one was claimed recently."""
    last = _last_attempt.get(instance_id)
    if last is not None and now - last < HEARTBEAT_INTERVAL_SECONDS:
        return False
    if len(_last_attempt) >= _MAX_TRACKED:
        cutoff = now - HEARTBEAT_INTERVAL_SECONDS
        for key in [k for k, v in _last_attempt.items() if v < cutoff]:
            del _last_attempt[key]
        if len(_last_attempt) >= _MAX_TRACKED:  # all fresh: drop the oldest half
            for key, _ in sorted(_last_attempt.items(), key=lambda kv: kv[1])[: _MAX_TRACKED // 2]:
                del _last_attempt[key]
    _last_attempt[instance_id] = now
    return True


def try_acquire(instance_id: UUID) -> bool:
    """Cheap in-process gate callers use BEFORE opening a session or thread."""
    return _due(instance_id, time.monotonic())


def record(db: Session, *, tenant_id: UUID, instance_id: UUID, throttled: bool = True) -> bool:
    """Upsert the heartbeat for an ALREADY-AUTHORIZED instance; throttled.

    ``throttled=False`` means the caller already passed :func:`try_acquire`.
    Returns True if a write was issued (not necessarily applied: the SQL guard
    may skip a row that is still fresh). Does not commit. The caller must have
    passed the console ownership gate; row-level security additionally pins the
    row to the session's current tenant.
    """
    if throttled and not try_acquire(instance_id):
        return False
    stmt = pg_insert(LabConsoleActivity).values(instance_id=instance_id, tenant_id=tenant_id, last_seen_at=func.now())
    stmt = stmt.on_conflict_do_update(
        index_elements=[LabConsoleActivity.instance_id],
        set_={"last_seen_at": func.now()},
        where=LabConsoleActivity.last_seen_at
        < func.now() - text(f"interval '{int(HEARTBEAT_INTERVAL_SECONDS)} seconds'"),
    )
    db.execute(stmt)
    return True


def reset_throttle() -> None:
    """Test hook: forget in-process rate-limit state."""
    global _last_warned
    _last_attempt.clear()
    _last_warned = None
