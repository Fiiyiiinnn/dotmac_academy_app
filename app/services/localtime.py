"""Academy wall-clock timezone — the single owner of local↔UTC conversion.

Instructors enter session times as wall-clock in the academy's timezone
(``settings.academy_timezone``, default Africa/Lagos). This module is the one
place that attaches that zone and converts to UTC for storage, and back for
display. Storage is always UTC; templates render via the ``localtime`` Jinja
filter registered in ``app/web/templating.py``.
"""

# Ported at new-composition genesis (delivery-core slice).

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

from app.config import settings


@lru_cache(maxsize=1)
def academy_zone() -> ZoneInfo:
    return ZoneInfo(settings.academy_timezone)


def local_to_utc(dt: datetime) -> datetime:
    """Interpret a naive wall-clock datetime as academy-local; return aware UTC.

    Aware datetimes are respected and converted, not reinterpreted.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=academy_zone())
    return dt.astimezone(ZoneInfo("UTC"))


def to_local(dt: datetime | None) -> datetime | None:
    """Convert an aware (UTC-stored) datetime to academy-local for display."""
    if dt is None:
        return None
    if dt.tzinfo is None:  # legacy naive rows are stored-UTC by convention
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(academy_zone())


def academy_tz_label(dt: datetime | None = None) -> str:
    """Short wall-clock zone label for UI copy (e.g. 'WAT' for Africa/Lagos).

    Derived from the configured zone at the given instant (falls back to the
    IANA name if the platform yields no abbreviation), so hardcoded "WAT"
    strings never drift when the academy timezone is reconfigured.
    """
    zone = academy_zone()
    ref = (dt or datetime.now(zone)).astimezone(zone)
    return ref.tzname() or settings.academy_timezone
