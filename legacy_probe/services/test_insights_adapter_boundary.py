"""Hosted-only checks of public insight facts at Academy's presentation boundary."""

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from dotmac_learning import (
    CohortComparison,
    CohortFunnel,
    CohortInsightTerms,
    CohortOverview,
    CurrentBlocker,
    DisciplineScore,
    DisciplineStrengths,
    GradeTrend,
    GradeTrendTerms,
    LabFailures,
    LearnerActivity,
    LearnerInsightTerms,
    ReportCohort,
    WeekBucket,
)
from jinja2 import ChoiceLoader, DictLoader

from app.services import insights
from app.web.templating import templates

LEARNER_TERMS = LearnerInsightTerms(8, 4, 7, GradeTrendTerms(10, 4, 0.05))
COHORT_TERMS = CohortInsightTerms(7, 4, 0.5)


@pytest.fixture(autouse=True)
def explicit_policy(monkeypatch):
    monkeypatch.setattr(insights, "learner_insight_terms", lambda db: LEARNER_TERMS)
    monkeypatch.setattr(insights, "cohort_insight_terms", lambda db: COHORT_TERMS)


def _render(name, **context):
    # Exercise the actual page template, with navigation/auth layout isolated.
    env = templates.env.overlay(
        loader=ChoiceLoader(
            [
                DictLoader({"base.html": "{% block content %}{% endblock %}"}),
                templates.env.loader,
            ]
        )
    )
    return env.get_template(name).render(**context)


@pytest.mark.parametrize(
    ("reason", "recommendation", "days", "text"),
    [
        ("overdue", "overdue", 2, "Submit this activity now — it is overdue."),
        (
            "attempts_exhausted",
            "attempts_exhausted",
            1,
            "Ask your instructor about a reset or guidance.",
        ),
        (
            None,
            "start_first_chapter",
            None,
            "Start your first chapter — everything begins there.",
        ),
        (
            None,
            "resume_after_week",
            9,
            "Pick up where you left off — a week away is easy to recover.",
        ),
        (
            None,
            "continue_unfinished",
            0,
            "Keep going: your next unfinished activity is on the dashboard.",
        ),
    ],
)
def test_learner_facts_render_existing_progress_contract(monkeypatch, reason, recommendation, days, text):
    session = object()
    tenant_id, person_id, course_id, activity_id = (uuid4() for _ in range(4))
    now = datetime(2026, 10, 17, 23, 30, tzinfo=UTC)
    due = now if reason == "overdue" else None
    blocker = CurrentBlocker(activity_id, "Checkpoint", course_id, reason, due) if reason else None
    fact = LearnerActivity(
        (WeekBucket(date(2026, 10, 3), 3), WeekBucket(date(2026, 10, 10), 0)),
        3,
        1.2,
        GradeTrend((20, 40, 80, 90), "up"),
        days,
        DisciplineStrengths(
            "Network",
            "Systems",
            (DisciplineScore("Network", 85), DisciplineScore("Systems", 42)),
        ),
        None,
        blocker,
        recommendation,
    )
    calls = []

    def owner(db, **kw):
        assert db is session
        assert kw == {"tenant_id": tenant_id, "person_id": person_id, "terms": LEARNER_TERMS, "now": now}
        calls.append(kw)
        return fact

    monkeypatch.setattr(insights.learning, "learner_activity", owner)
    activity = insights.learner_activity(session, tenant_id=tenant_id, person_id=person_id, now=now)

    assert len(calls) == 1
    assert activity["weekly"] == [
        {"week_start": date(2026, 10, 3), "count": 3},
        {"week_start": date(2026, 10, 10), "count": 0},
    ]
    assert activity["max_week"] == 3 and activity["velocity_per_week"] == 1.2
    assert activity["grade_trend"] == {"points": [20, 40, 80, 90], "direction": "up"}
    assert activity["disciplines"] == {
        "strong": "Network",
        "weak": "Systems",
        "all": {"Network": 85, "Systems": 42},
    }
    assert activity["days_since_last"] == days and activity["attendance"] is None
    assert activity["recommendation"] == text
    if reason:
        assert activity["blocker"]["activity_id"] == activity_id
        assert activity["blocker"]["course_id"] == course_id
        assert activity["blocker"]["due_at"] == due
        assert activity["blocker"]["action"] == text
        assert activity["blocker"]["rule"] == (
            "due date passed without a passing score" if reason == "overdue" else "maximum attempts used without a pass"
        )
    else:
        assert activity["blocker"] is None
    html = _render("progress.html", activity=activity, overview=[])
    assert text in html and "Improving" in html
    assert "Strongest area:" in html and "Network" in html


def test_empty_learner_facts_forward_default_time_without_new_policy(monkeypatch):
    session = object()
    tenant_id, person_id = uuid4(), uuid4()
    fact = LearnerActivity(
        (),
        0,
        0.0,
        GradeTrend((), "flat"),
        None,
        DisciplineStrengths(None, None, ()),
        None,
        None,
        "start_first_chapter",
    )

    def owner(db, **kw):
        assert db is session
        assert kw == {"tenant_id": tenant_id, "person_id": person_id, "terms": LEARNER_TERMS, "now": None}
        return fact

    monkeypatch.setattr(insights.learning, "learner_activity", owner)
    activity = insights.learner_activity(session, tenant_id=tenant_id, person_id=person_id)
    assert activity["weekly"] == [] and activity["max_week"] == 0
    assert activity["grade_trend"] == {"points": [], "direction": "flat"}
    assert activity["disciplines"] == {"strong": None, "weak": None, "all": {}}
    assert activity["blocker"] is None and activity["days_since_last"] is None
    html = _render("progress.html", activity=activity, overview=[])
    assert "Start your first chapter" in html and "Strongest area:" not in html


def test_cohort_facts_keep_nullable_values_and_legacy_nested_collections(monkeypatch):
    session = object()
    tenant_id, cohort_id = uuid4(), uuid4()
    now = datetime(2026, 10, 17, tzinfo=UTC)
    fact = CohortOverview(
        8,
        3,
        5,
        7,
        19,
        4,
        6,
        None,
        CohortFunnel(8, 7, 5, 2, 1),
        (LabFailures(None, None, 9), LabFailures(uuid4(), "", 2)),
        31,
        4.2,
    )

    def owner(db, **kw):
        assert db is session
        assert kw == {"tenant_id": tenant_id, "cohort_id": cohort_id, "terms": COHORT_TERMS, "now": now}
        return fact

    monkeypatch.setattr(insights.learning, "insight_cohort_overview", owner)
    value = insights.cohort_overview(session, tenant_id=tenant_id, cohort_id=cohort_id, now=now)
    assert value == {
        "learners": 8,
        "active": 3,
        "inactive": 5,
        "active_rule": "any learning event in the last 7 days",
        "submitted": 19,
        "overdue": 4,
        "not_attempted": 6,
        "avg_score_pct": None,
        "funnel": {
            "enrolled": 8,
            "started": 7,
            "half": 5,
            "completed": 2,
            "certified": 1,
        },
        "lab_failures": [
            {"course": "—", "failed_checks": 9},
            {"course": "", "failed_checks": 2},
        ],
        "week_events": 31,
        "prior_week_avg": 4.2,
    }


def test_comparison_and_csv_helper_preserve_owner_order_and_duplicates(monkeypatch):
    session = object()
    tenant_id, cohort_id, course_id = uuid4(), uuid4(), uuid4()
    now = datetime(2026, 10, 17, tzinfo=UTC)
    # Equal case-folded names retain the owner's encounter order.
    first = ReportCohort(uuid4(), tenant_id, "Alpha", "network", "active", "online", now, now)
    second = ReportCohort(uuid4(), tenant_id, "ALPHA", "systems", "active", "online", now, now)
    facts = (CohortComparison(first, 4, 3, 2, 50), CohortComparison(second, 0, 0, 0, 0))

    def comparison(db, **kw):
        assert db is session
        assert kw == {"tenant_id": tenant_id, "active_window_days": COHORT_TERMS.active_window_days, "now": now}
        return facts

    courses = [course_id, course_id, uuid4()]

    def course_ids(db, **kw):
        assert db is session
        assert kw == {"tenant_id": tenant_id, "cohort_id": cohort_id}
        return courses

    monkeypatch.setattr(insights.learning, "cohorts_comparison", comparison)
    monkeypatch.setattr(insights.learning, "cohort_course_ids", course_ids)
    rows = insights.cohorts_comparison(session, tenant_id=tenant_id, now=now)
    assert isinstance(rows, list)
    assert rows[0]["cohort"] is first and rows[1]["cohort"] is second
    assert rows[0]["completion_pct"] == 50 and rows[1]["completion_pct"] == 0
    assert rows[1]["learners"] == 0 and rows[1]["completed"] == 0
    assert insights._cohort_course_ids(session, tenant_id, cohort_id) is courses
    html = _render("instructor/reports_index.html", comparison=rows, cohorts=[], insight_terms=COHORT_TERMS)
    assert html.index("Alpha") < html.index("ALPHA")
    assert "50%" in html and "0%" in html


def test_configured_resume_label_uses_supplied_policy_days(monkeypatch):
    terms = LearnerInsightTerms(3, 2, 11, GradeTrendTerms(6, 2, 0.25))
    monkeypatch.setattr(insights, "learner_insight_terms", lambda db: terms)
    fact = LearnerActivity(
        (), 0, 0.0, GradeTrend((), "flat"), 11, DisciplineStrengths(None, None, ()), None, None, "resume_after_week"
    )

    def owner(db, **kw):
        assert kw["terms"] is terms
        return fact

    monkeypatch.setattr(insights.learning, "learner_activity", owner)
    result = insights.learner_activity(object(), tenant_id=uuid4(), person_id=uuid4())
    assert result["recommendation"] == "Pick up where you left off — 11 days away is easy to recover."
