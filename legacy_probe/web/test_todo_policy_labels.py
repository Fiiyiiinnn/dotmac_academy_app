"""Actual todo template fragment consumes the same policy used for its facts.

These render canaries cover product copy, not installed identity or authorization.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader, StrictUndefined


@pytest.mark.parametrize(
    ("near", "far", "horizon", "empty_copy"),
    [(7, 30, 30, "Nothing due this week."), (2, 11, 9, "Nothing due in the next 2 days.")],
)
def test_actual_todo_template_uses_configured_windows_and_preserves_default_copy(near, far, horizon, empty_copy):
    template_root = Path(__file__).resolve().parents[2] / "templates"
    env = Environment(
        loader=ChoiceLoader(
            [DictLoader({"base.html": "{% block content %}{% endblock %}"}), FileSystemLoader(template_root)]
        ),
        undefined=StrictUndefined,
        autoescape=True,
    )
    env.filters["localtime"] = lambda value: value
    timeline = {
        "near_deadline_days": near,
        "far_deadline_days": far,
        "window_horizon_days": horizon,
        "overdue": [],
        "due_today": [],
        "next_7": [],
        "next_30": [],
        "sessions": [],
        "joinable": set(),
        "graded": [],
        "feedback": [],
        "course_windows": [],
    }
    rendered = env.get_template("todo/index.html").render(
        timeline=timeline,
        branding_name="Academy",
        academy_tz_label="UTC",
        new_feed_token=None,
        has_feed_token=False,
        request=SimpleNamespace(state=SimpleNamespace(csrf_token="fixture-token")),
    )
    assert f"Next {near} days" in rendered
    assert f"Next {far} days" in rendered
    assert f"No course openings or closings in the next {horizon} days." in rendered
    assert empty_copy in rendered
    if near != 7:
        assert "Nothing due this week." not in rendered
