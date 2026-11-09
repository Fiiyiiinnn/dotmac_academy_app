"""Wall-clock timezone owner: local↔UTC conversion (roadmap P0 item 1)."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.services.localtime import local_to_utc, to_local


def test_naive_input_is_academy_wall_clock():
    # Lagos is UTC+1 year-round (no DST).
    utc = local_to_utc(datetime(2026, 8, 1, 10, 0))
    assert utc.tzinfo is not None
    assert utc.astimezone(UTC).hour == 9


def test_aware_input_is_converted_not_reinterpreted():
    aware = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
    assert local_to_utc(aware) == aware


def test_round_trip_lagos_wall_clock():
    wall = datetime(2026, 8, 1, 10, 0)
    back = to_local(local_to_utc(wall))
    assert (back.year, back.month, back.day, back.hour, back.minute) == (2026, 8, 1, 10, 0)
    assert back.tzinfo == ZoneInfo("Africa/Lagos")


def test_to_local_treats_legacy_naive_as_utc():
    local = to_local(datetime(2026, 8, 1, 9, 0))
    assert local.hour == 10  # UTC+1


def test_to_local_none_passthrough():
    assert to_local(None) is None
