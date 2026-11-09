"""Academy product settings (environment-backed).

Only Academy-unique knobs live here; platform concerns (tenancy,
identity, sessions, domain settings) are kernel-owned. DB-stored
operator settings arrive with the kernel settings port; until then
`app.lab_settings.effective` reads these env values directly.
"""

from __future__ import annotations

import math
from typing import Annotated

from dotmac_kernel.config import Settings as KernelSettings
from dotmac_kernel.config import validate_settings as validate_kernel_settings
from pydantic import Field, field_validator
from pydantic_settings import NoDecode, SettingsConfigDict
from sqlalchemy.engine import make_url

from app.admissions_settings import (
    ADMISSION_ENROLLMENT_POLICY_KEYS,
    AUTHENTICATION_POLICY_SHAPES,
    BOOTSTRAP_POLICY_KEYS,
    ENROLLMENT_INITIAL_STATE_POLICY_KEYS,
    ENTRANCE_INVITATION_POLICY_KEYS,
    ICAL_FEED_POLICY_KEYS,
    INITIAL_IDENTITY_POLICY_KEYS,
    INVITATION_PURPOSE_POLICY_SHAPES,
    LEARNING_POLICY_SHAPES,
    ONBOARDING_POLICY_KEYS,
    PASSWORD_CHANGE_POLICY_SHAPES,
    PROFILE_POLICY_SHAPES,
    InvitationMemberRole,
    validate_admin_home_access_roles,
    validate_admission_enrollment_policy_value,
    validate_authentication_policy_value,
    validate_bank_author_roles,
    validate_bank_export_roles,
    validate_bank_publish_roles,
    validate_bank_repoint_roles,
    validate_bank_retire_roles,
    validate_bootstrap_policy_value,
    validate_catalog_staff_roles,
    validate_enrollment_initial_state_policy_value,
    validate_entrance_invitation_deadline_days,
    validate_gradebook_access_roles,
    validate_ical_feed_policy,
    validate_initial_identity_policy_value,
    validate_invitation_default_member_role,
    validate_invitation_member_role_map,
    validate_invitation_purpose_policy_value,
    validate_lab_admin_access_roles,
    validate_learning_policy_value,
    validate_notifications_recent_limit,
    validate_onboarding_policy_value,
    validate_password_change_policy_value,
    validate_password_reset_min_length,
    validate_profile_policy_value,
    validate_reminders_admin_access_roles,
    validate_reports_access_roles,
    validate_settings_access_roles,
    validate_teaching_access_roles,
    validate_web_account_creation_password_min_length,
    validate_web_account_creation_role_admission,
    validate_web_account_creation_role_provisioning,
)
from app.lab_policy import (
    LAB_CAPACITY_KEYS,
    LAB_EXECUTION_POLICY_SHAPES,
    LabExecutionPolicy,
    validate_lab_check_output_limit,
    validate_lab_execution_value,
    validate_lab_policy_integer,
)

__all__ = ["LabExecutionPolicy", "Settings", "settings"]


class Settings(KernelSettings):
    model_config = SettingsConfigDict(extra="ignore", hide_input_in_errors=True)

    academy_timezone: str = "Africa/Lagos"
    platform_root_domain: str = ""
    smtp_from: str = "Dotmac Academy <academy@localhost>"
    smtp_host: str = ""
    smtp_starttls: bool = True
    metrics_token: str = ""

    # Operator-policy defaults. These fields are the single defaults
    # source (mirrors the legacy architecture): the kernel-backed
    # settings store derives its defaults from a pristine Settings and
    # stored platform rows override them per key.
    admission_enrollment_initial_role: str = "student"
    admission_enrollment_initial_status: str = "active"
    bulk_enrollment_initial_role: str = "student"
    bulk_enrollment_initial_status: str = "active"
    learning_agenda_limit: int = 50
    learning_dashboard_at_risk_fraction: float = 0.5
    learning_dashboard_stale_days: int = 14
    learning_default_activity_assessment_mode: str = "graded"
    learning_default_activity_grading: str = "auto"
    learning_default_activity_pass_threshold: float = 0.0
    learning_default_activity_type: str = "mcq_test"
    learning_default_activity_weight: float = 1.0
    learning_instructor_at_risk_fraction: float = 0.5
    learning_instructor_stale_days: int = 14
    learning_instructor_workspace_limit: int = 12
    learning_lab_pass_threshold: float = 0.7
    learning_search_staff_roles: Annotated[tuple[str, ...], NoDecode] = Field(
        default_factory=lambda: ("instructor", "admin")
    )
    account_avatar_max_bytes: int = 1048576
    account_email_digest_default: bool = True
    account_email_results_default: bool = True
    account_initial_is_active: bool = True
    account_initial_profile_status: str = "active"
    admin_home_access_roles: Annotated[tuple[str, ...], NoDecode] = Field(default_factory=lambda: ("admin",))
    authentication_lockout_minutes: float = 15.0
    authentication_max_failed_logins: int = 5
    bank_author_roles: Annotated[tuple[str, ...], NoDecode] = Field(default_factory=lambda: ("instructor", "admin"))
    bank_export_roles: Annotated[tuple[str, ...], NoDecode] = Field(default_factory=lambda: ("admin",))
    bank_publish_roles: Annotated[tuple[str, ...], NoDecode] = Field(default_factory=lambda: ("admin",))
    bank_repoint_roles: Annotated[tuple[str, ...], NoDecode] = Field(default_factory=lambda: ("admin",))
    bank_retire_roles: Annotated[tuple[str, ...], NoDecode] = Field(default_factory=lambda: ("admin",))
    bootstrap_allowed_database_role_pairs: Annotated[tuple[str, ...], NoDecode] = Field(default_factory=lambda: ())
    bootstrap_initial_role_slug: str = "admin"
    bootstrap_owner_first_name: str = "Admin"
    bootstrap_owner_last_name_template: str = "{tenant_name}"
    bootstrap_password_min_length: int = 0
    bootstrap_role_provisioning: Annotated[dict[str, object], NoDecode] = Field(
        default_factory=lambda: {"student": "Student", "instructor": "Instructor", "admin": "Administrator"}
    )
    bootstrap_tenant_is_active: bool = True
    branding_name: str = "Dotmac Academy"
    catalog_staff_roles: Annotated[tuple[str, ...], NoDecode] = Field(default_factory=lambda: ("instructor", "admin"))
    email_auto_on_pass: bool = True
    email_digest_enabled: bool = True
    entrance_invitation_deadline_days: int = 7
    entrance_questions_per_category: int = 0
    gradebook_access_roles: Annotated[tuple[str, ...], NoDecode] = Field(
        default_factory=lambda: ("instructor", "admin")
    )
    ical_feed_filename: str = "dotmac-academy.ics"
    ical_feed_token_ttl_days: int = 365
    ical_feed_uid_domain: str = "academy.dotmac.io"
    invitation_default_member_role: str = "student"
    invitation_enrollment_initial_status: str = "active"
    invitation_human_replace_outstanding: bool = True
    invitation_member_role_map: Annotated[dict[str, object], NoDecode] = Field(
        default_factory=lambda: {"admin": "instructor", "instructor": "instructor"}
    )
    invitation_onboarding_replace_outstanding: bool = True
    invitation_password_min_length: int = 8
    invitation_stranded_replace_outstanding: bool = False
    invitation_token_hours: float = 168.0
    lab_admin_access_roles: Annotated[tuple[str, ...], NoDecode] = Field(
        default_factory=lambda: ("instructor", "admin")
    )
    lab_execution_policy_overrides: Annotated[dict[str, object], NoDecode] = Field(default_factory=lambda: {})
    learner_digest_enabled: bool = False
    learning_active_window_days: int = 7
    learning_activity_chart_weeks: int = 8
    learning_activity_report_hours: int = 24
    learning_attention_limit: int = 8
    learning_bank_fallback_pass_threshold: float = 0.0
    learning_bank_new_activity_assessment_mode: str = "graded"
    learning_bank_new_activity_grading: str = "auto"
    learning_bank_new_activity_weight: float = 1.0
    learning_bank_pass_threshold_by_kind: Annotated[dict[str, object], NoDecode] = Field(
        default_factory=lambda: {"chapter": 0.0, "final": 0.7, "mid": 0.6}
    )
    learning_behind_pace_fraction: float = 0.2
    learning_cohort_half_fraction: float = 0.5
    learning_comparison_prior_weeks: int = 4
    learning_default_activity_scoring_policy: str = "best"
    learning_default_attempt_cooldown_seconds: int = 0
    learning_default_certificate_auto_issue: bool = True
    learning_default_certificate_validity_days: int = 0
    learning_default_completion_fraction: float = 1.0
    learning_default_course_listed: bool = False
    learning_default_course_status: str = "published"
    learning_default_extra_attempts: int = 1
    learning_default_session_type: str = "live_class"
    learning_exam_fallback_duration_seconds: float = 360.0
    learning_exam_fallback_score_floor: float = 0.3333333333333333
    learning_exam_near_chance_margin: float = 1.33
    learning_exam_seconds_per_question: float = 12.0
    learning_feedback_reveal_by_mode: Annotated[dict[str, object], NoDecode] = Field(
        default_factory=lambda: {"exam": "never", "graded": "on_pass_or_exhausted", "practice": "immediate"}
    )
    learning_join_lead_minutes: int = 60
    learning_missing_question_weight: int = 1
    learning_pacing_default_open: bool = True
    learning_person_announcements_limit: int = 20
    learning_prerequisite_default_composition: str = "all"
    learning_prerequisite_default_kind: str = "course_completed"
    learning_progress_trend_min_delta_fraction: float = 0.03
    learning_progress_trend_min_samples: int = 3
    learning_progress_trend_sample_limit: int = 8
    learning_reading_minimum_minutes: int = 1
    learning_reading_words_per_minute: int = 200
    learning_resume_after_days: int = 7
    learning_search_limit: int = 20
    learning_session_promotes_delivery_mode: bool = True
    learning_tenant_announcements_limit: int = 50
    learning_todo_far_deadline_days: int = 30
    learning_todo_graded_limit: int = 10
    learning_todo_graded_lookback_days: int = 14
    learning_todo_near_deadline_days: int = 7
    learning_todo_window_horizon_days: int = 30
    learning_top_movers_limit: int = 5
    learning_track_default_gating_mode: str = "previous"
    learning_trend_min_delta_fraction: float = 0.05
    learning_trend_min_samples: int = 4
    learning_trend_sample_limit: int = 10
    learning_velocity_weeks: int = 4
    management_inquiry_recipient: str = "fiberacademy@dotmac.ng"
    notifications_recent_limit: int = 30
    onboarding_enrollment_global_role: str = "student"
    onboarding_task_catalog: Annotated[tuple[tuple[str, str], ...], NoDecode] = Field(
        default_factory=lambda: (
            ("entrance_assessment", "Complete the entrance assessment"),
            ("confirm_details", "Confirm your enrolment details"),
            ("orientation", "Review the programme orientation"),
        )
    )
    password_change_min_length: int = 8
    password_reset_min_length: int = 8
    password_reset_token_minutes: float = 120.0
    reminder_digest_hour: int = 7
    reminder_digest_weekday: int = 0
    reminder_inactivity_days: int = 7
    reminder_inactivity_max_nudges: int = 4
    reminders_admin_access_roles: Annotated[tuple[str, ...], NoDecode] = Field(default_factory=lambda: ("admin",))
    reminders_enabled: bool = True
    reports_access_roles: Annotated[tuple[str, ...], NoDecode] = Field(default_factory=lambda: ("instructor", "admin"))
    settings_access_roles: Annotated[tuple[str, ...], NoDecode] = Field(default_factory=lambda: ("admin",))
    smtp_port: int = 587
    smtp_user: str = ""
    success_almost_complete_fraction: float = 0.8
    success_below_passing_high_grade_factor: float = 0.5
    success_below_passing_min_graded: int = 2
    success_below_passing_min_weight_fraction: float = 0.2
    success_certificate_grace_hours: int = 6
    success_enabled_rules: Annotated[tuple[str, ...], NoDecode] = Field(
        default_factory=lambda: ("inactivity", "overdue_work", "below_passing", "failed_final", "certificate_blocked")
    )
    success_inactive_days: int = 7
    success_inactivity_high_factor: float = 2.0
    success_min_grade_pct: int = 60
    success_overdue_high_count: int = 3
    success_overdue_high_days: int = 7
    success_overdue_title_limit: int = 5
    success_severity_by_rule: Annotated[dict[str, object], NoDecode] = Field(
        default_factory=lambda: {"certificate_blocked": "medium", "failed_final": "high"}
    )
    teaching_access_roles: Annotated[tuple[str, ...], NoDecode] = Field(default_factory=lambda: ("instructor", "admin"))
    web_account_creation_password_min_length: int = 0
    web_account_creation_role_admission: Annotated[dict[str, object], NoDecode] = Field(
        default_factory=lambda: {"student": ("instructor", "admin"), "instructor": ("admin",), "admin": ("admin",)}
    )
    web_account_creation_role_provisioning: Annotated[dict[str, object], NoDecode] = Field(
        default_factory=lambda: {"student": "Student", "instructor": "Instructor", "admin": "Administrator"}
    )
    glitchtip_dsn: str = ""
    glitchtip_enabled: bool = False
    glitchtip_traces_sample_rate: float = 0.1
    metrics_lab_probe_ttl_seconds: float = 30.0
    metrics_lab_probe_timeout_seconds: float = 2.0
    metrics_pipeline_ttl_seconds: float = 60.0
    # SMTP password is env-only by policy (never a stored setting);
    # the remaining SMTP coordinates are operator settings in the
    # kernel-backed settings store.
    smtp_password: str = ""

    # ERP integration (inert when URL/secrets are empty).
    academy_public_base_url: str = ""
    erp_allowed_return_origins: str = ""
    erp_assessment_token_secret: str = ""
    erp_inbound_hmac_max_skew_seconds: int = 300
    erp_inbound_hmac_secret: str = ""
    erp_webhook_secret: str = ""
    erp_webhook_url: str = ""
    # App-sync pilot (completions to dotmac_erp_v2): "off" | "shadow".
    # Shadow mirrors delivered completions as envelopes; it never stamps
    # the checkpoint and never changes the legacy webhook outcome.
    erp_app_sync_mode: str = "off"
    erp_app_sync_url: str = ""
    erp_app_sync_source_scope_ref: str = "dotmac"

    max_concurrent_labs: int = 20
    max_concurrent_labs_per_tenant: int = 0
    lab_command_timeout_seconds_by_action: Annotated[dict[str, int], NoDecode] = Field(
        default_factory=lambda: {"inspect": 60, "deploy": 900, "destroy": 420, "check": 480}
    )
    lab_operation_lease_seconds_by_kind: Annotated[dict[str, int], NoDecode] = Field(
        default_factory=lambda: {"deploy": 1200, "destroy": 600, "check": 600}
    )
    lab_operation_max_attempts_by_kind: Annotated[dict[str, int], NoDecode] = Field(
        default_factory=lambda: {"deploy": 3, "destroy": 5, "check": 3}
    )
    lab_operation_retry_delay_seconds: int = 5
    lab_worker_poll_interval_seconds: int = 5
    lab_restart_reclaim_lock_timeout_seconds: int = 5
    lab_operation_unknown_lease_seconds: int = 600
    lab_operation_unknown_max_attempts: int = 3
    lab_process_termination_grace_seconds: int = 5

    @field_validator(*LAB_EXECUTION_POLICY_SHAPES, mode="before")
    @classmethod
    def _lab_execution_value(cls, value: object, info):
        return validate_lab_execution_value(info.field_name, value)

    @field_validator(*LAB_CAPACITY_KEYS, mode="before")
    @classmethod
    def _lab_capacity_value(cls, value: object, info):
        return validate_lab_policy_integer(info.field_name, value)

    lab_check_evidence_chars: int = 160
    lab_check_dns_actual_chars: int = 120
    lab_workdir: str = "/home/dotmac/labs"
    lab_host_role: str = "web"
    lab_worker_database_url: str = ""
    lab_idle_minutes: int = 60
    lab_console_host: str = "127.0.0.1"
    lab_console_bind_host: str = ""
    lab_console_max_body_bytes: int = 1_048_576
    lab_host_probe_port: int = 22

    @field_validator(*ICAL_FEED_POLICY_KEYS, mode="before")
    @classmethod
    def _ical_feed_policy(cls, value: object, info):
        return validate_ical_feed_policy(info.field_name, value)

    @field_validator("notifications_recent_limit", mode="before")
    @classmethod
    def _notifications_recent_limit(cls, value: object) -> int:
        return validate_notifications_recent_limit(value)

    @field_validator("admin_home_access_roles", mode="before")
    @classmethod
    def _admin_home_access_roles(cls, value: object) -> tuple[str, ...]:
        return validate_admin_home_access_roles(value)

    @field_validator("lab_admin_access_roles", mode="before")
    @classmethod
    def _lab_admin_access_roles(cls, value: object) -> tuple[str, ...]:
        return validate_lab_admin_access_roles(value)

    @field_validator("reminders_admin_access_roles", mode="before")
    @classmethod
    def _reminders_admin_access_roles(cls, value: object) -> tuple[str, ...]:
        return validate_reminders_admin_access_roles(value)

    @field_validator("teaching_access_roles", mode="before")
    @classmethod
    def _teaching_access_roles(cls, value: object) -> tuple[str, ...]:
        return validate_teaching_access_roles(value)

    @field_validator("catalog_staff_roles", mode="before")
    @classmethod
    def _catalog_staff_roles(cls, value: object) -> tuple[str, ...]:
        return validate_catalog_staff_roles(value)

    @field_validator("gradebook_access_roles", mode="before")
    @classmethod
    def _gradebook_access_roles(cls, value: object) -> tuple[str, ...]:
        return validate_gradebook_access_roles(value)

    @field_validator("settings_access_roles", mode="before")
    @classmethod
    def _settings_access_roles(cls, value: object) -> tuple[str, ...]:
        return validate_settings_access_roles(value)

    @field_validator("reports_access_roles", mode="before")
    @classmethod
    def _reports_access_roles(cls, value: object) -> tuple[str, ...]:
        return validate_reports_access_roles(value)

    @field_validator("bank_author_roles", mode="before")
    @classmethod
    def _bank_author_roles(cls, value: object) -> tuple[str, ...]:
        return validate_bank_author_roles(value)

    @field_validator("bank_publish_roles", mode="before")
    @classmethod
    def _bank_publish_roles(cls, value: object) -> tuple[str, ...]:
        return validate_bank_publish_roles(value)

    @field_validator("bank_repoint_roles", mode="before")
    @classmethod
    def _bank_repoint_roles(cls, value: object) -> tuple[str, ...]:
        return validate_bank_repoint_roles(value)

    @field_validator("bank_export_roles", mode="before")
    @classmethod
    def _bank_export_roles(cls, value: object) -> tuple[str, ...]:
        return validate_bank_export_roles(value)

    @field_validator("bank_retire_roles", mode="before")
    @classmethod
    def _bank_retire_roles(cls, value: object) -> tuple[str, ...]:
        return validate_bank_retire_roles(value)

    @field_validator(*LEARNING_POLICY_SHAPES, mode="before")
    @classmethod
    def _learning_policy_value(cls, value: object, info) -> object:
        return validate_learning_policy_value(info.field_name, value)

    @field_validator(*BOOTSTRAP_POLICY_KEYS, mode="before")
    @classmethod
    def _bootstrap_policy_value(cls, value: object, info):
        return validate_bootstrap_policy_value(info.field_name, value)

    @field_validator(*INITIAL_IDENTITY_POLICY_KEYS, mode="before")
    @classmethod
    def _initial_identity_policy_value(cls, value: object, info) -> bool | str:
        return validate_initial_identity_policy_value(info.field_name, value)

    @field_validator(*PROFILE_POLICY_SHAPES, mode="before")
    @classmethod
    def _profile_policy_value(cls, value: object, info) -> int | bool:
        return validate_profile_policy_value(info.field_name, value)

    @field_validator(*AUTHENTICATION_POLICY_SHAPES, mode="before")
    @classmethod
    def _authentication_policy_value(cls, value: object, info) -> int | float:
        return validate_authentication_policy_value(info.field_name, value)

    @field_validator(*PASSWORD_CHANGE_POLICY_SHAPES, mode="before")
    @classmethod
    def _password_change_policy_value(cls, value: object, info) -> int:
        return validate_password_change_policy_value(info.field_name, value)

    @field_validator("password_reset_min_length", mode="before")
    @classmethod
    def _password_reset_minimum(cls, value: object) -> int:
        return validate_password_reset_min_length(value)

    @field_validator(*ENTRANCE_INVITATION_POLICY_KEYS, mode="before")
    @classmethod
    def _entrance_invitation_policy_value(cls, value: object) -> int:
        return validate_entrance_invitation_deadline_days(value)

    @field_validator(*ADMISSION_ENROLLMENT_POLICY_KEYS, mode="before")
    @classmethod
    def _admission_enrollment_policy_value(cls, value: object, info) -> str:
        return validate_admission_enrollment_policy_value(info.field_name, value)

    @field_validator(*ENROLLMENT_INITIAL_STATE_POLICY_KEYS, mode="before")
    @classmethod
    def _enrollment_initial_state_policy_value(cls, value: object, info) -> str:
        return validate_enrollment_initial_state_policy_value(info.field_name, value)

    @field_validator(*ONBOARDING_POLICY_KEYS, mode="before")
    @classmethod
    def _onboarding_policy_value(cls, value: object, info) -> object:
        return validate_onboarding_policy_value(info.field_name, value)

    @field_validator(*INVITATION_PURPOSE_POLICY_SHAPES, mode="before")
    @classmethod
    def _invitation_purpose_policy_value(cls, value: object, info) -> int | float | bool:
        return validate_invitation_purpose_policy_value(info.field_name, value)

    @field_validator("invitation_member_role_map", mode="before")
    @classmethod
    def _invitation_role_mapping(cls, value: object) -> dict[str, InvitationMemberRole]:
        return validate_invitation_member_role_map(value)

    @field_validator("invitation_default_member_role", mode="before")
    @classmethod
    def _invitation_role_default(cls, value: object) -> InvitationMemberRole:
        return validate_invitation_default_member_role(value)

    @field_validator("web_account_creation_role_admission", mode="before")
    @classmethod
    def _web_account_role_admission(cls, value: object) -> dict[str, tuple[str, ...]]:
        return validate_web_account_creation_role_admission(value)

    @field_validator("web_account_creation_password_min_length", mode="before")
    @classmethod
    def _web_account_password_minimum(cls, value: object) -> int:
        return validate_web_account_creation_password_min_length(value)

    @field_validator("web_account_creation_role_provisioning", mode="before")
    @classmethod
    def _web_account_role_provisioning(cls, value: object) -> dict[str, str]:
        return validate_web_account_creation_role_provisioning(value)

    @field_validator("lab_check_evidence_chars", "lab_check_dns_actual_chars", mode="before")
    @classmethod
    def _lab_check_output_limit(cls, value: object) -> int:
        return validate_lab_check_output_limit(value)

    @field_validator(
        "metrics_lab_probe_ttl_seconds",
        "metrics_lab_probe_timeout_seconds",
        "metrics_pipeline_ttl_seconds",
        mode="before",
    )
    @classmethod
    def _metrics_positive_duration(cls, value: object) -> float:
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ValueError("metrics durations must be finite positive numbers")
        try:
            duration = float(value)
        except (ValueError, OverflowError):
            raise ValueError("metrics durations must be finite positive numbers") from None
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("metrics durations must be finite positive numbers")
        return duration


# Legacy validator surface: product policy validators live in
# `app.admissions_settings` (extracted unchanged from the legacy config);
# they are re-exported here because `app.config` was their historical
# import site for services and tests.
from app.admissions_settings import *  # noqa: F403,E402
from app.admissions_settings import (  # noqa: E402,F401
    ADMISSION_ENROLLMENT_POLICY_KEYS,
    AUTHENTICATION_POLICY_SHAPES,
    BOOTSTRAP_POLICY_KEYS,
    ENROLLMENT_INITIAL_STATE_POLICY_KEYS,
    ENTRANCE_INVITATION_POLICY_KEYS,
    ICAL_FEED_POLICY_KEYS,
    INITIAL_IDENTITY_POLICY_KEYS,
    INVITATION_PURPOSE_POLICY_SHAPES,
    LEARNING_POLICY_SHAPES,
    ONBOARDING_POLICY_KEYS,
    ONBOARDING_PROCEDURES,
    PASSWORD_CHANGE_POLICY_SHAPES,
    PROFILE_POLICY_SHAPES,
)

settings = Settings()


def certificate_validity_days_or_none(value: int) -> int | None:
    """Map the stored sentinel to Learning validity: 0 means never expires."""
    if type(value) is not int or value < 0:
        raise ValueError("learning_default_certificate_validity_days has an invalid policy value")
    return None if value == 0 else value


# The proxied ttyd/webfig console documents are served under a path-scoped
# policy (kernel capsule, deploy gate) — see CSP addendum 2026-10-27.
ACADEMY_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; base-uri 'self'; frame-ancestors 'none'; "
    "form-action 'self'; object-src 'none'; img-src 'self' data:; "
    "font-src 'self'; style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; connect-src 'self'"
)


def validate_settings(s: Settings) -> list[str]:
    """Return list of fatal errors (empty if OK). Caller raises if non-empty in prod."""
    errors = validate_kernel_settings(s)
    try:
        LabExecutionPolicy.from_settings(s)
    except ValueError:
        errors.append("Lab execution policy is invalid")
    # Single-tenancy is the kernel's control (TENANCY=single asserts exactly
    # one tenant row at startup). Production must still declare the posture.
    if s.is_production and s.tenancy != "single":
        errors.append(
            "TENANCY must be 'single' in production — this is a single-tenant "
            "deployment and the kernel's startup assertion is what enforces it"
        )
    if s.is_production and not s.csrf_enabled:
        errors.append("CSRF_ENABLED must be true in production")
    if s.is_production and not s.rate_limit_enabled:
        errors.append("RATE_LIMIT_ENABLED must be true in production")
    if s.rate_limit_requests <= 0 or s.rate_limit_window_seconds <= 0:
        errors.append("rate-limit request and window values must be positive")
    if s.max_concurrent_labs <= 0:
        errors.append("MAX_CONCURRENT_LABS must be positive")
    if s.max_concurrent_labs_per_tenant < 0:
        errors.append("MAX_CONCURRENT_LABS_PER_TENANT must be zero or positive")
    lab_role = s.lab_host_role.lower()
    if lab_role not in {"web", "lab"}:
        errors.append("LAB_HOST_ROLE must be 'web' or 'lab'")
    elif lab_role == "lab":
        if not s.lab_worker_database_url:
            errors.append("LAB_WORKER_DATABASE_URL is required when LAB_HOST_ROLE=lab")
        else:
            try:
                worker_role = make_url(s.lab_worker_database_url).username
            except Exception:
                worker_role = None
            if worker_role != "academy_lab_worker":
                errors.append("LAB_WORKER_DATABASE_URL must authenticate as academy_lab_worker")
    if s.is_production and s.smtp_host and not s.smtp_starttls:
        errors.append("SMTP_STARTTLS must be true when SMTP is configured in production")
    if s.erp_inbound_hmac_max_skew_seconds <= 0:
        errors.append("ERP_INBOUND_HMAC_MAX_SKEW_SECONDS must be positive")
    if s.erp_inbound_hmac_secret:
        if not s.erp_assessment_token_secret:
            errors.append("ERP_ASSESSMENT_TOKEN_SECRET is required when ERP inbound integration is enabled")
        if not s.erp_allowed_return_origins:
            errors.append("ERP_ALLOWED_RETURN_ORIGINS is required when ERP inbound integration is enabled")
        if not s.academy_public_base_url:
            errors.append("ACADEMY_PUBLIC_BASE_URL is required when ERP inbound integration is enabled")
        if s.erp_webhook_secret and s.erp_inbound_hmac_secret == s.erp_webhook_secret:
            errors.append("ERP inbound and outbound HMAC secrets must be distinct")
    if s.is_production and s.academy_public_base_url and not s.academy_public_base_url.startswith("https://"):
        errors.append("ACADEMY_PUBLIC_BASE_URL must use HTTPS in production")
    return errors
