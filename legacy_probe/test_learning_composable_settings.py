"""A1 composable Learning settings: catalog, defaults, refusals, drift guard.

Pure composition tests run without a database by stubbing the settings
store read; they pin that every A1 default reproduces the historical
hardcoded behaviour and that malformed explicit policy never falls back.
"""

import pytest

from app.config import (
    LEARNING_POLICY_SHAPES,
    Settings,
    certificate_validity_days_or_none,
    settings,
    validate_learning_policy_relations,
    validate_learning_policy_value,
)
from app.services import learning_policy, settings_store

A1_KEYS = (
    "learning_prerequisite_default_kind",
    "learning_prerequisite_default_composition",
    "learning_track_default_gating_mode",
    "learning_default_completion_fraction",
    "learning_default_certificate_auto_issue",
    "learning_default_certificate_validity_days",
    "learning_feedback_reveal_by_mode",
    "learning_default_activity_scoring_policy",
    "learning_progress_trend_sample_limit",
    "learning_progress_trend_min_samples",
    "learning_progress_trend_min_delta_fraction",
    "learning_cohort_half_fraction",
    "success_enabled_rules",
    "success_severity_by_rule",
    "learning_session_promotes_delivery_mode",
    "learning_default_attempt_cooldown_seconds",
    "learning_pacing_default_open",
)

LEGACY_DEFAULTS = {
    "learning_prerequisite_default_kind": "course_completed",
    "learning_prerequisite_default_composition": "all",
    "learning_track_default_gating_mode": "previous",
    "learning_default_completion_fraction": 1.0,
    "learning_default_certificate_auto_issue": True,
    "learning_default_certificate_validity_days": 0,
    "learning_feedback_reveal_by_mode": {
        "practice": "immediate",
        "graded": "on_pass_or_exhausted",
        "exam": "never",
    },
    "learning_default_activity_scoring_policy": "best",
    "learning_progress_trend_sample_limit": 8,
    "learning_progress_trend_min_samples": 3,
    "learning_progress_trend_min_delta_fraction": 0.03,
    "learning_cohort_half_fraction": 0.5,
    "success_enabled_rules": (
        "inactivity",
        "overdue_work",
        "below_passing",
        "failed_final",
        "certificate_blocked",
    ),
    "success_severity_by_rule": {"failed_final": "high", "certificate_blocked": "medium"},
    "learning_session_promotes_delivery_mode": True,
    "learning_default_attempt_cooldown_seconds": 0,
    "learning_pacing_default_open": True,
}


@pytest.fixture
def empty_store(monkeypatch):
    monkeypatch.setattr(settings_store, "get_all", lambda db: {})
    return object()


def test_a1_catalog_is_complete_in_shapes_settings_and_store():
    assert len(A1_KEYS) == 17
    for key in A1_KEYS:
        assert key in LEARNING_POLICY_SHAPES, key
        assert key in settings_store.KNOWN_KEYS, key
        assert hasattr(settings, key), key
    # Stored overrides flow end-to-end with no settings_store edit: the
    # store coerces every LEARNING_POLICY_SHAPES key through the validator.
    assert set(A1_KEYS) <= set(LEARNING_POLICY_SHAPES)


def test_a1_defaults_reproduce_legacy_values():
    fresh = Settings()
    for key, expected in LEGACY_DEFAULTS.items():
        assert getattr(fresh, key) == expected, key
        assert validate_learning_policy_value(key, getattr(fresh, key)) == expected, key


def test_a1_defaults_match_learning_server_defaults():
    from dotmac_learning.models import Activity, Course
    from dotmac_learning.participation_models import CourseOffering, CoursePrerequisite, Track

    def column(model, name):
        return model.__table__.c[name]

    assert column(CoursePrerequisite, "condition_kind").default.arg == settings.learning_prerequisite_default_kind
    assert column(Course, "prerequisite_composition").default.arg == settings.learning_prerequisite_default_composition
    assert column(Track, "gating_mode").default.arg == settings.learning_track_default_gating_mode
    assert column(Course, "completion_fraction").default.arg == settings.learning_default_completion_fraction
    assert column(Course, "certificate_auto_issue").default.arg is settings.learning_default_certificate_auto_issue
    assert column(Course, "certificate_validity_days").default is None
    assert certificate_validity_days_or_none(settings.learning_default_certificate_validity_days) is None
    assert column(Activity, "scoring_policy").default.arg == settings.learning_default_activity_scoring_policy
    assert (
        column(Activity, "min_attempt_interval_seconds").default.arg
        == settings.learning_default_attempt_cooldown_seconds
    )
    assert column(CourseOffering, "pacing_default_open").default.arg is settings.learning_pacing_default_open


def test_certificate_validity_sentinel_mapping():
    assert certificate_validity_days_or_none(0) is None
    assert certificate_validity_days_or_none(1) == 1
    assert certificate_validity_days_or_none(365) == 365
    for bad in (-1, True, 1.5, "30"):
        with pytest.raises(ValueError):
            certificate_validity_days_or_none(bad)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("learning_prerequisite_default_kind", "waived"),
        ("learning_prerequisite_default_kind", ""),
        ("learning_prerequisite_default_composition", "some"),
        ("learning_track_default_gating_mode", "next"),
        ("learning_default_completion_fraction", "0"),
        ("learning_default_completion_fraction", "1.5"),
        ("learning_default_completion_fraction", "nan"),
        ("learning_default_certificate_auto_issue", "maybe"),
        ("learning_default_certificate_validity_days", "-1"),
        ("learning_default_certificate_validity_days", "1.5"),
        ("learning_feedback_reveal_by_mode", '{"practice":"immediate","graded":"never","exam":"immediate"}'),
        ("learning_feedback_reveal_by_mode", '{"practice":"immediate","graded":"never"}'),
        ("learning_feedback_reveal_by_mode", '{"practice":"immediate","graded":"never","exam":"never","lab":"never"}'),
        ("learning_feedback_reveal_by_mode", '{"practice":"later","graded":"never","exam":"never"}'),
        ("learning_feedback_reveal_by_mode", '{"practice":"immediate","practice":"never","graded":"never","exam":"never"}'),
        ("learning_feedback_reveal_by_mode", None),
        ("learning_default_activity_scoring_policy", "average"),
        ("learning_progress_trend_sample_limit", "1"),
        ("learning_progress_trend_min_samples", "0"),
        ("learning_progress_trend_min_delta_fraction", "-0.1"),
        ("learning_progress_trend_min_delta_fraction", "1.1"),
        ("learning_cohort_half_fraction", "2"),
        ("success_enabled_rules", '["inactivity","unknown_rule"]'),
        ("success_enabled_rules", '["inactivity","inactivity"]'),
        ("success_enabled_rules", "inactivity"),
        ("success_severity_by_rule", '{"unknown_rule":"high"}'),
        ("success_severity_by_rule", '{"failed_final":"critical"}'),
        ("success_severity_by_rule", '{"failed_final":1}'),
        ("learning_session_promotes_delivery_mode", "perhaps"),
        ("learning_default_attempt_cooldown_seconds", "-1"),
        ("learning_default_attempt_cooldown_seconds", "2592001"),
        ("learning_pacing_default_open", None),
    ],
)
def test_a1_malformed_or_closed_catalog_violations_refuse(key, value):
    with pytest.raises(ValueError):
        validate_learning_policy_value(key, value)


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("learning_prerequisite_default_kind", "certificate_held", "certificate_held"),
        ("learning_prerequisite_default_composition", "any", "any"),
        ("learning_track_default_gating_mode", "all_previous", "all_previous"),
        ("learning_default_completion_fraction", "0.5", 0.5),
        ("learning_default_certificate_validity_days", "90", 90),
        (
            "learning_feedback_reveal_by_mode",
            '{"practice":"never","graded":"immediate","exam":"on_pass_or_exhausted"}',
            {"practice": "never", "graded": "immediate", "exam": "on_pass_or_exhausted"},
        ),
        ("learning_default_activity_scoring_policy", "latest", "latest"),
        ("success_enabled_rules", '["inactivity"]', ("inactivity",)),
        ("success_enabled_rules", "[]", ()),
        ("success_severity_by_rule", "{}", {}),
        ("success_severity_by_rule", '{"inactivity":"low"}', {"inactivity": "low"}),
        ("learning_default_attempt_cooldown_seconds", "2592000", 2592000),
    ],
)
def test_a1_nondefault_opt_in_values_validate(key, value, expected):
    assert validate_learning_policy_value(key, value) == expected


def test_progress_trend_relation_refuses_sample_below_minimum():
    values = {key: getattr(settings, key) for key in LEARNING_POLICY_SHAPES}
    validate_learning_policy_relations(values)
    values["learning_progress_trend_sample_limit"] = 2
    values["learning_progress_trend_min_samples"] = 5
    with pytest.raises(ValueError, match="progress trend"):
        validate_learning_policy_relations(values)


def test_composers_at_defaults_build_legacy_terms(empty_store):
    feedback = learning_policy.feedback_reveal_terms(empty_store)
    assert dict(feedback.reveal_by_mode) == LEGACY_DEFAULTS["learning_feedback_reveal_by_mode"]
    trend = learning_policy.progress_trend_terms(empty_store)
    assert (trend.sample_limit, trend.min_samples, trend.min_delta_fraction) == (8, 3, 0.03)
    cohort = learning_policy.cohort_insight_terms(empty_store)
    assert cohort.half_fraction == 0.5
    success = learning_policy.success_terms(empty_store)
    assert success.enabled_rules == LEGACY_DEFAULTS["success_enabled_rules"]
    assert dict(success.severity_by_rule) == LEGACY_DEFAULTS["success_severity_by_rule"]
    bank = learning_policy.bank_activity_terms(empty_store)
    assert bank.new_activity_scoring_policy == "best"
    assert bank.new_activity_min_attempt_interval_seconds == 0
    defaults = learning_policy.creation_defaults(empty_store)
    assert defaults.prerequisite_default_kind == "course_completed"
    assert defaults.prerequisite_default_composition == "all"
    assert defaults.track_default_gating_mode == "previous"
    assert defaults.completion_fraction == 1.0
    assert defaults.certificate_auto_issue is True
    assert defaults.certificate_validity_days is None
    assert defaults.activity_scoring_policy == "best"
    assert defaults.attempt_cooldown_seconds == 0
    assert defaults.session_promotes_delivery_mode is True
    assert defaults.pacing_default_open is True


def test_sensitive_composers_require_the_store(monkeypatch):
    for composer in (
        learning_policy.feedback_reveal_terms,
        learning_policy.progress_trend_terms,
        learning_policy.success_terms,
        learning_policy.bank_activity_terms,
        learning_policy.cohort_insight_terms,
        learning_policy.creation_defaults,
    ):
        with pytest.raises(ValueError, match="session"):
            composer(None)  # type: ignore[arg-type]

    def fail_read(db):
        raise RuntimeError("policy store unavailable")

    monkeypatch.setattr(settings_store, "get_all", fail_read)
    with pytest.raises(RuntimeError, match="policy store unavailable"):
        learning_policy.feedback_reveal_terms(object())
    with pytest.raises(RuntimeError, match="policy store unavailable"):
        learning_policy.creation_defaults(object())


def test_learning_terms_themselves_refuse_invalid_templates():
    """Template failures: Learning Terms construction is the second fence."""
    import dotmac_learning as learning

    with pytest.raises(ValueError):
        learning.FeedbackRevealTerms(
            reveal_by_mode={"practice": "immediate", "graded": "never", "exam": "immediate"}
        )
    with pytest.raises(ValueError):
        learning.FeedbackRevealTerms(reveal_by_mode={"practice": "immediate"})
    with pytest.raises(ValueError):
        learning.ProgressTrendTerms(sample_limit=2, min_samples=5, min_delta_fraction=0.03)
    with pytest.raises(ValueError):
        learning.CohortInsightTerms(active_window_days=7, comparison_prior_weeks=4, half_fraction=1.5)


def test_stored_malformed_a1_value_never_falls_back(monkeypatch):
    monkeypatch.setattr(
        settings_store,
        "get_all",
        lambda db: {"learning_feedback_reveal_by_mode": '{"practice":"immediate","graded":"never","exam":"immediate"}'},
    )
    with pytest.raises(ValueError):
        settings_store.effective(object(), require_store=True)
    with pytest.raises(ValueError):
        learning_policy.feedback_reveal_terms(object())
