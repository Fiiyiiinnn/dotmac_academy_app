# Ported at new-composition genesis (web layer wave).
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.services.localtime import academy_tz_label, to_local
from app.ui import template_globals
from app.web.context import nav_context

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR), context_processors=[nav_context])
# Render stored-UTC datetimes as academy wall-clock (see app/services/localtime).
templates.env.filters["localtime"] = to_local


def _datefmt(dt):
    """Compact human date-time for dashboard chips, e.g. 'Aug 03, 14:00'."""
    if dt is None:
        return ""
    return dt.strftime("%b %d, %H:%M")


templates.env.filters["datefmt"] = _datefmt


def _elapsed(dt: datetime | None) -> str:
    """Compact age for polling surfaces, e.g. '42s ago' or '3 min ago'.

    Stored timestamps are UTC; a naive value is read as UTC rather than
    crashing a page render. Future timestamps (clock skew) clamp to
    'just now' instead of rendering a negative age.
    """
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    seconds = max(0, int((datetime.now(UTC) - dt).total_seconds()))
    if seconds < 5:
        return "just now"
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} hr ago"
    days = seconds // 86400
    return f"{days} day{'s' if days != 1 else ''} ago"


templates.env.filters["elapsed"] = _elapsed
# Dynamic wall-clock zone label (e.g. "WAT") for UI copy — no hardcoded strings.
templates.env.globals["academy_tz_label"] = academy_tz_label()
templates.env.globals.update(template_globals())
