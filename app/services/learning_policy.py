"""Compose reusable Learning terms from the strict Academy settings store."""

from dataclasses import dataclass
from typing import cast

import dotmac_learning as learning
from sqlalchemy.orm import Session

from app.config import certificate_validity_days_or_none
from app.services.settings_store import effective


def todo_terms(db: Session) -> learning.TodoTerms:
    cfg = effective(db, require_store=True)
    return learning.TodoTerms(
        graded_lookback_days=cast(int, cfg["learning_todo_graded_lookback_days"]),
        graded_limit=cast(int, cfg["learning_todo_graded_limit"]),
        window_horizon_days=cast(int, cfg["learning_todo_window_horizon_days"]),
        near_deadline_days=cast(int, cfg["learning_todo_near_deadline_days"]),
        far_deadline_days=cast(int, cfg["learning_todo_far_deadline_days"]),
    )


def grade_trend_terms(db: Session) -> learning.GradeTrendTerms:
    cfg = effective(db, require_store=True)
    return learning.GradeTrendTerms(
        sample_limit=cast(int, cfg["learning_trend_sample_limit"]),
        min_samples=cast(int, cfg["learning_trend_min_samples"]),
        min_delta_fraction=cast(float, cfg["learning_trend_min_delta_fraction"]),
    )


def learner_insight_terms(db: Session) -> learning.LearnerInsightTerms:
    cfg = effective(db, require_store=True)
    return learning.LearnerInsightTerms(
        chart_weeks=cast(int, cfg["learning_activity_chart_weeks"]),
        velocity_weeks=cast(int, cfg["learning_velocity_weeks"]),
        resume_after_days=cast(int, cfg["learning_resume_after_days"]),
        grade_trend=learning.GradeTrendTerms(
            sample_limit=cast(int, cfg["learning_trend_sample_limit"]),
            min_samples=cast(int, cfg["learning_trend_min_samples"]),
            min_delta_fraction=cast(float, cfg["learning_trend_min_delta_fraction"]),
        ),
    )


def cohort_insight_terms(db: Session) -> learning.CohortInsightTerms:
    cfg = effective(db, require_store=True)
    return learning.CohortInsightTerms(
        active_window_days=cast(int, cfg["learning_active_window_days"]),
        comparison_prior_weeks=cast(int, cfg["learning_comparison_prior_weeks"]),
        half_fraction=cast(float, cfg["learning_cohort_half_fraction"]),
    )


def success_terms(db: Session) -> learning.SuccessTerms:
    cfg = effective(db, require_store=True)
    return learning.SuccessTerms(
        inactive_days=cast(int, cfg["success_inactive_days"]),
        min_grade_pct=cast(int, cfg["success_min_grade_pct"]),
        certificate_grace_hours=cast(int, cfg["success_certificate_grace_hours"]),
        inactivity_high_factor=cast(float, cfg["success_inactivity_high_factor"]),
        overdue_high_count=cast(int, cfg["success_overdue_high_count"]),
        overdue_high_days=cast(int, cfg["success_overdue_high_days"]),
        overdue_title_limit=cast(int, cfg["success_overdue_title_limit"]),
        below_passing_min_graded=cast(int, cfg["success_below_passing_min_graded"]),
        below_passing_min_weight_fraction=cast(float, cfg["success_below_passing_min_weight_fraction"]),
        below_passing_high_grade_factor=cast(float, cfg["success_below_passing_high_grade_factor"]),
        almost_complete_fraction=cast(float, cfg["success_almost_complete_fraction"]),
        enabled_rules=cast(tuple[str, ...], cfg["success_enabled_rules"]),
        severity_by_rule=cast(dict[str, str], cfg["success_severity_by_rule"]),
    )


def bank_activity_terms(db: Session) -> learning.BankActivityTerms:
    cfg = effective(db, require_store=True)
    return learning.BankActivityTerms(
        pass_threshold_by_kind=cast(dict[str, float], cfg["learning_bank_pass_threshold_by_kind"]),
        fallback_pass_threshold=cast(float, cfg["learning_bank_fallback_pass_threshold"]),
        new_activity_assessment_mode=cast(str, cfg["learning_bank_new_activity_assessment_mode"]),
        new_activity_grading=cast(str, cfg["learning_bank_new_activity_grading"]),
        new_activity_weight=cast(float, cfg["learning_bank_new_activity_weight"]),
        new_activity_scoring_policy=cast(str, cfg["learning_default_activity_scoring_policy"]),
        new_activity_min_attempt_interval_seconds=cast(int, cfg["learning_default_attempt_cooldown_seconds"]),
    )


def validity_terms(db: Session) -> learning.ValidityTerms:
    cfg = effective(db, require_store=True)
    return learning.ValidityTerms(
        seconds_per_question=cast(float, cfg["learning_exam_seconds_per_question"]),
        near_chance_margin=cast(float, cfg["learning_exam_near_chance_margin"]),
        fallback_score_floor=cast(float, cfg["learning_exam_fallback_score_floor"]),
        fallback_duration_seconds=cast(float, cfg["learning_exam_fallback_duration_seconds"]),
    )


def feedback_reveal_terms(db: Session) -> learning.FeedbackRevealTerms:
    """Sensitive answer-key policy; a missing store must never fall back."""
    cfg = effective(db, require_store=True)
    return learning.FeedbackRevealTerms(
        reveal_by_mode=cast(dict[str, str], cfg["learning_feedback_reveal_by_mode"]),
    )


def progress_trend_terms(db: Session) -> learning.ProgressTrendTerms:
    cfg = effective(db, require_store=True)
    return learning.ProgressTrendTerms(
        sample_limit=cast(int, cfg["learning_progress_trend_sample_limit"]),
        min_samples=cast(int, cfg["learning_progress_trend_min_samples"]),
        min_delta_fraction=cast(float, cfg["learning_progress_trend_min_delta_fraction"]),
    )


@dataclass(frozen=True, slots=True)
class LearningCreationDefaults:
    """Creation-time defaults for A2 call sites, composed in one snapshot."""

    prerequisite_default_kind: str
    prerequisite_default_composition: str
    track_default_gating_mode: str
    completion_fraction: float
    certificate_auto_issue: bool
    certificate_validity_days: int | None
    activity_scoring_policy: str
    attempt_cooldown_seconds: int
    session_promotes_delivery_mode: bool
    pacing_default_open: bool


def creation_defaults(db: Session) -> LearningCreationDefaults:
    """Access/scoring creation defaults; require the settings store."""
    cfg = effective(db, require_store=True)
    return LearningCreationDefaults(
        prerequisite_default_kind=cast(str, cfg["learning_prerequisite_default_kind"]),
        prerequisite_default_composition=cast(str, cfg["learning_prerequisite_default_composition"]),
        track_default_gating_mode=cast(str, cfg["learning_track_default_gating_mode"]),
        completion_fraction=cast(float, cfg["learning_default_completion_fraction"]),
        certificate_auto_issue=cast(bool, cfg["learning_default_certificate_auto_issue"]),
        certificate_validity_days=certificate_validity_days_or_none(
            cast(int, cfg["learning_default_certificate_validity_days"])
        ),
        activity_scoring_policy=cast(str, cfg["learning_default_activity_scoring_policy"]),
        attempt_cooldown_seconds=cast(int, cfg["learning_default_attempt_cooldown_seconds"]),
        session_promotes_delivery_mode=cast(bool, cfg["learning_session_promotes_delivery_mode"]),
        pacing_default_open=cast(bool, cfg["learning_pacing_default_open"]),
    )


def reading_minutes(db: Session, word_count: int) -> int:
    """Estimate presentation time using composed Academy product policy."""
    cfg = effective(db, require_store=True)
    return max(
        cast(int, cfg["learning_reading_minimum_minutes"]),
        round(word_count / cast(int, cfg["learning_reading_words_per_minute"])),
    )
