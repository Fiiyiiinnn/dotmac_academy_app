"""Learning policy composition: explicit overrides, refusal and transaction ownership."""

from dataclasses import FrozenInstanceError

import pytest

from app.config import settings, validate_learning_policy_value
from app.models.platform_settings import PlatformSetting
from app.services import learning_policy, settings_store


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("learning_join_lead_minutes", None),
        ("learning_join_lead_minutes", True),
        ("learning_join_lead_minutes", "1.5"),
        ("learning_join_lead_minutes", "-1"),
        ("learning_exam_near_chance_margin", "0"),
        ("learning_exam_seconds_per_question", "nan"),
        ("learning_bank_pass_threshold_by_kind", '{"chapter":0.2,"chapter":0.3}'),
        ("learning_bank_pass_threshold_by_kind", '{"chapter":true}'),
        ("learning_default_course_listed", "maybe"),
        ("success_almost_complete_fraction", "1.1"),
    ],
)
def test_explicit_malformed_policy_never_becomes_a_default(key, value):
    with pytest.raises(ValueError):
        validate_learning_policy_value(key, value)


def test_legacy_signed_success_policy_remains_explicit():
    assert validate_learning_policy_value("success_inactive_days", "-1") == -1
    assert validate_learning_policy_value("success_min_grade_pct", "-2") == -2
    assert validate_learning_policy_value("success_certificate_grace_hours", "-3") == -3
    assert validate_learning_policy_value("learning_exam_seconds_per_question", "0") == 0


def test_factories_refuse_missing_session_and_failed_store(monkeypatch):
    with pytest.raises(ValueError, match="session"):
        learning_policy.todo_terms(None)

    def fail_read(db):
        raise RuntimeError("policy store unavailable")

    monkeypatch.setattr(settings_store, "get_all", fail_read)
    with pytest.raises(RuntimeError, match="policy store unavailable"):
        learning_policy.todo_terms(object())


def test_product_compatibility_defaults_live_in_settings(monkeypatch):
    monkeypatch.setattr(settings, "learning_join_lead_minutes", 17)
    monkeypatch.setattr(settings_store, "get_all", lambda db: {})
    assert settings_store.effective(object(), require_store=True)["learning_join_lead_minutes"] == 17
    assert settings.learning_agenda_limit == 50


def test_stored_null_policy_is_not_an_absent_override(admin_session):
    admin_session.merge(PlatformSetting(key="learning_join_lead_minutes", value=None))
    admin_session.flush()
    assert "learning_join_lead_minutes" in settings_store.get_all(admin_session)
    with pytest.raises(ValueError):
        settings_store.effective(admin_session, require_store=True)
    admin_session.rollback()


def test_database_overrides_all_composed_terms_without_committing(admin_session, monkeypatch):
    admin_session.connection()  # begin the root explicitly (no autobegin yet)
    root = admin_session.get_transaction()
    assert root is not None
    monkeypatch.setattr(settings, "learning_todo_graded_limit", 99)
    settings_store.set_many(
        admin_session,
        {
            "learning_todo_graded_limit": "3",
            "learning_todo_near_deadline_days": "2",
            "learning_todo_far_deadline_days": "11",
            "learning_trend_sample_limit": "6",
            "learning_trend_min_samples": "3",
            "learning_activity_chart_weeks": "5",
            "learning_velocity_weeks": "2",
            "learning_resume_after_days": "11",
            "learning_active_window_days": "4",
            "learning_comparison_prior_weeks": "2",
            "success_inactive_days": "-1",
            "success_overdue_title_limit": "2",
            "learning_bank_pass_threshold_by_kind": '{"chapter":0.25}',
            "learning_bank_fallback_pass_threshold": "0.45",
            "learning_exam_seconds_per_question": "0",
            "learning_exam_near_chance_margin": "1.5",
        },
    )
    todo = learning_policy.todo_terms(admin_session)
    assert todo.graded_limit == 3
    assert (todo.near_deadline_days, todo.far_deadline_days) == (2, 11)
    trend = learning_policy.grade_trend_terms(admin_session)
    assert (trend.sample_limit, trend.min_samples) == (6, 3)
    learner = learning_policy.learner_insight_terms(admin_session)
    assert (learner.chart_weeks, learner.velocity_weeks, learner.resume_after_days) == (5, 2, 11)
    assert learner.grade_trend == trend
    cohort = learning_policy.cohort_insight_terms(admin_session)
    assert (cohort.active_window_days, cohort.comparison_prior_weeks) == (4, 2)
    success = learning_policy.success_terms(admin_session)
    assert (success.inactive_days, success.overdue_title_limit) == (-1, 2)
    bank = learning_policy.bank_activity_terms(admin_session)
    assert dict(bank.pass_threshold_by_kind) == {"chapter": 0.25}
    assert bank.fallback_pass_threshold == 0.45
    with pytest.raises(TypeError):
        bank.pass_threshold_by_kind["chapter"] = 0.9
    validity = learning_policy.validity_terms(admin_session)
    assert (validity.seconds_per_question, validity.near_chance_margin) == (0, 1.5)
    with pytest.raises(FrozenInstanceError):
        validity.near_chance_margin = 2.0
    assert admin_session.get_transaction() is root
    assert root.is_active
    admin_session.rollback()


def test_contradictory_policy_write_refuses_before_partial_mutation(admin_session):
    settings_store.set_many(admin_session, {"learning_todo_near_deadline_days": "2"})
    root = admin_session.get_transaction()
    with pytest.raises(ValueError, match="near deadline"):
        settings_store.set_many(
            admin_session,
            {"learning_todo_near_deadline_days": "40", "learning_todo_far_deadline_days": "3"},
        )
    assert settings_store.effective(admin_session, require_store=True)["learning_todo_near_deadline_days"] == 2
    assert admin_session.get_transaction() is root
    assert root.is_active
    admin_session.rollback()


@pytest.mark.parametrize("broken", [None, "malformed", "-1"])
def test_valid_replacement_repairs_malformed_stored_policy(admin_session, broken):
    admin_session.merge(PlatformSetting(key="learning_join_lead_minutes", value=broken))
    admin_session.flush()
    with pytest.raises(ValueError):
        settings_store.effective(admin_session, require_store=True)
    settings_store.set_many(admin_session, {"learning_join_lead_minutes": "19"})
    assert settings_store.effective(admin_session, require_store=True)["learning_join_lead_minutes"] == 19
    admin_session.rollback()


def test_valid_replacement_repairs_inconsistent_stored_policy(admin_session):
    admin_session.merge(PlatformSetting(key="learning_todo_near_deadline_days", value="40"))
    admin_session.merge(PlatformSetting(key="learning_todo_far_deadline_days", value="3"))
    admin_session.flush()
    with pytest.raises(ValueError, match="near deadline"):
        settings_store.effective(admin_session, require_store=True)
    settings_store.set_many(
        admin_session,
        {"learning_todo_near_deadline_days": "4", "learning_todo_far_deadline_days": "13"},
    )
    terms = learning_policy.todo_terms(admin_session)
    assert (terms.near_deadline_days, terms.far_deadline_days) == (4, 13)
    admin_session.rollback()


def test_delete_refuses_inconsistent_fallback_before_mutation(admin_session):
    settings_store.set_many(
        admin_session,
        {"learning_todo_near_deadline_days": "40", "learning_todo_far_deadline_days": "50"},
    )
    root = admin_session.get_transaction()
    with pytest.raises(ValueError, match="near deadline"):
        settings_store.delete(admin_session, "learning_todo_far_deadline_days")
    assert settings_store.get_all(admin_session)["learning_todo_far_deadline_days"] == "50"
    assert admin_session.get_transaction() is root
    assert root.is_active
    settings_store.delete(admin_session, "learning_todo_near_deadline_days")
    assert learning_policy.todo_terms(admin_session).near_deadline_days == settings.learning_todo_near_deadline_days
    admin_session.rollback()


def test_report_lab_and_session_policy_store_overrides_and_rejects_unknown_kind(admin_session):
    settings_store.set_many(
        admin_session,
        {
            "learning_activity_report_hours": "19",
            "learning_lab_pass_threshold": "0.85",
            "learning_default_session_type": "lab",
        },
    )
    root = admin_session.get_transaction()
    cfg = settings_store.effective(admin_session, require_store=True)
    assert cfg["learning_activity_report_hours"] == 19
    assert cfg["learning_lab_pass_threshold"] == 0.85
    assert cfg["learning_default_session_type"] == "lab"
    with pytest.raises(ValueError):
        settings_store.set_many(admin_session, {"learning_default_session_type": "unsupported-session-kind"})
    assert settings_store.get_all(admin_session)["learning_default_session_type"] == "lab"
    assert admin_session.get_transaction() is root
    assert root.is_active
    admin_session.rollback()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("learning_activity_report_hours", None),
        ("learning_activity_report_hours", "1.5"),
        ("learning_lab_pass_threshold", None),
        ("learning_lab_pass_threshold", "nan"),
        ("learning_lab_pass_threshold", "1.1"),
        ("learning_default_session_type", "unsupported-session-kind"),
    ],
)
def test_new_explicit_policy_values_never_fall_back(key, value):
    with pytest.raises(ValueError):
        validate_learning_policy_value(key, value)


# --- A1 composable settings (store-level) ---------------------------------


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("learning_prerequisite_default_kind", "waived"),
        ("learning_track_default_gating_mode", "next"),
        ("learning_default_completion_fraction", "0"),
        ("learning_feedback_reveal_by_mode", '{"practice":"immediate","graded":"never","exam":"immediate"}'),
        ("learning_default_activity_scoring_policy", "average"),
        ("learning_default_attempt_cooldown_seconds", "2592001"),
        ("success_enabled_rules", '["not_a_rule"]'),
        ("success_severity_by_rule", '{"failed_final":"critical"}'),
    ],
)
def test_a1_explicit_policy_values_never_fall_back(key, value):
    with pytest.raises(ValueError):
        validate_learning_policy_value(key, value)


def test_a1_database_overrides_compose_new_terms_without_committing(admin_session):
    admin_session.connection()  # begin the root explicitly (no autobegin yet)
    root = admin_session.get_transaction()
    assert root is not None
    settings_store.set_many(
        admin_session,
        {
            "learning_cohort_half_fraction": "0.25",
            "success_enabled_rules": '["failed_final"]',
            "success_severity_by_rule": '{"failed_final":"low"}',
            "learning_default_activity_scoring_policy": "latest",
            "learning_default_attempt_cooldown_seconds": "60",
            "learning_feedback_reveal_by_mode": '{"practice":"never","graded":"immediate","exam":"never"}',
            "learning_progress_trend_sample_limit": "6",
            "learning_progress_trend_min_samples": "2",
            "learning_progress_trend_min_delta_fraction": "0.1",
        },
    )
    assert learning_policy.cohort_insight_terms(admin_session).half_fraction == 0.25
    success = learning_policy.success_terms(admin_session)
    assert success.enabled_rules == ("failed_final",)
    assert dict(success.severity_by_rule) == {"failed_final": "low"}
    bank = learning_policy.bank_activity_terms(admin_session)
    assert bank.new_activity_scoring_policy == "latest"
    assert bank.new_activity_min_attempt_interval_seconds == 60
    feedback = learning_policy.feedback_reveal_terms(admin_session)
    assert dict(feedback.reveal_by_mode) == {"practice": "never", "graded": "immediate", "exam": "never"}
    trend = learning_policy.progress_trend_terms(admin_session)
    assert (trend.sample_limit, trend.min_samples, trend.min_delta_fraction) == (6, 2, 0.1)
    assert admin_session.get_transaction() is root
    assert root.is_active
    admin_session.rollback()


def test_a1_creation_defaults_map_validity_sentinel(admin_session):
    defaults = learning_policy.creation_defaults(admin_session)
    assert defaults.certificate_validity_days is None
    settings_store.set_many(admin_session, {"learning_default_certificate_validity_days": "45"})
    assert learning_policy.creation_defaults(admin_session).certificate_validity_days == 45
    admin_session.rollback()


def test_a1_contradictory_progress_trend_write_refuses(admin_session):
    with pytest.raises(ValueError, match="progress trend"):
        settings_store.set_many(
            admin_session,
            {"learning_progress_trend_sample_limit": "2", "learning_progress_trend_min_samples": "5"},
        )
    admin_session.rollback()


def test_a1_exam_immediate_store_write_refuses(admin_session):
    with pytest.raises(ValueError):
        settings_store.set_many(
            admin_session,
            {"learning_feedback_reveal_by_mode": '{"practice":"immediate","graded":"never","exam":"immediate"}'},
        )
    admin_session.rollback()
