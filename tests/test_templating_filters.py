"""Presentation filters owned by Academy's Jinja environment."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.web.templating import templates


def _elapsed(value):
    return templates.env.filters["elapsed"](value)


def test_elapsed_filter_formats_recent_and_older_timestamps():
    now = datetime.now(UTC)
    assert _elapsed(None) == ""
    assert _elapsed(now) == "just now"
    assert _elapsed(now - timedelta(seconds=42)) == "42s ago"
    assert _elapsed(now - timedelta(minutes=3, seconds=10)) == "3 min ago"
    assert _elapsed(now - timedelta(hours=2, minutes=4)) == "2 hr ago"
    assert _elapsed(now - timedelta(days=2, hours=3)) == "2 days ago"


def test_elapsed_filter_treats_naive_timestamps_as_utc_and_clamps_future():
    now = datetime.now(UTC)
    naive = (now - timedelta(minutes=2)).replace(tzinfo=None)
    assert _elapsed(naive) == "2 min ago"
    assert _elapsed(now + timedelta(minutes=5)) == "just now"
