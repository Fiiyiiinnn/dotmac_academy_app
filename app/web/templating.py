# Ported at new-composition genesis (web layer wave).
from __future__ import annotations

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
# Dynamic wall-clock zone label (e.g. "WAT") for UI copy — no hardcoded strings.
templates.env.globals["academy_tz_label"] = academy_tz_label()
templates.env.globals.update(template_globals())
