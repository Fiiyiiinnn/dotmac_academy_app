"""Admissions/invitation settings validators (product-owned).

Extracted unchanged from the legacy app config at new-composition
genesis; the policies in `app.services` validate stored/env values
through these before use, refusing malformed values outright.
"""

from __future__ import annotations

import json
import math
import sys
from datetime import timedelta
from typing import Literal, cast

ENTRANCE_INVITATION_POLICY_KEYS = ("entrance_invitation_deadline_days",)


def validate_entrance_invitation_deadline_days(value: object) -> int:
    """Parse Settings/store integers without bool/float or malformed fallback."""
    message = "entrance_invitation_deadline_days must be a positive integer"
    if isinstance(value, str) and value.strip().isascii() and value.strip().isdecimal():
        try:
            value = int(value.strip())
        except ValueError:
            raise ValueError(message) from None
    if type(value) is not int or value < 1:
        raise ValueError(message)
    return value


INVITATION_PURPOSE_POLICY_SHAPES = {
    "invitation_token_hours": "signed_hours",
    "invitation_password_min_length": "positive_int",
    "invitation_human_replace_outstanding": "bool",
    "invitation_stranded_replace_outstanding": "bool",
    "invitation_onboarding_replace_outstanding": "bool",
}


def validate_invitation_purpose_policy_value(key: str, value: object) -> int | float | bool:
    """Strict invitation purpose policy; no clamping or malformed fallback."""
    shape = INVITATION_PURPOSE_POLICY_SHAPES[key]
    message = f"{key} has an invalid policy value"
    if shape == "bool":
        if isinstance(value, str):
            text = value.strip().lower()
            if text in {"true", "1", "yes", "on"}:
                return True
            if text in {"false", "0", "no", "off"}:
                return False
        if type(value) is not bool:
            raise ValueError(message)
        return value
    if shape == "positive_int":
        if isinstance(value, str) and value.strip().isascii() and value.strip().isdecimal():
            try:
                value = int(value.strip())
            except ValueError:
                raise ValueError(message) from None
        if type(value) is not int or value < 1:
            raise ValueError(message)
        return value
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except (ValueError, OverflowError):
            raise ValueError(message) from None
    if type(value) not in (int, float):
        raise ValueError(message)
    try:
        normalized = float(cast(int | float, value))
        if not math.isfinite(normalized):
            raise ValueError(message)
        timedelta(hours=normalized)
    except (ValueError, OverflowError):
        raise ValueError(message) from None
    return normalized


ADMISSION_ENROLLMENT_POLICY_KEYS = (
    "admission_enrollment_initial_role",
    "admission_enrollment_initial_status",
)


def validate_admission_enrollment_policy_value(key: str, value: object) -> str:
    """Match Learning's unconstrained String(20), without normalization."""
    if key not in ADMISSION_ENROLLMENT_POLICY_KEYS or type(value) is not str or len(value) > 20:
        raise ValueError(f"{key} must be a string of at most 20 characters")
    return value


BULK_ENROLLMENT_POLICY_KEYS = (
    "bulk_enrollment_initial_role",
    "bulk_enrollment_initial_status",
)


INVITATION_ENROLLMENT_POLICY_KEYS = ("invitation_enrollment_initial_status",)


ENROLLMENT_INITIAL_STATE_POLICY_KEYS = (*BULK_ENROLLMENT_POLICY_KEYS, *INVITATION_ENROLLMENT_POLICY_KEYS)


def validate_enrollment_initial_state_policy_value(key: str, value: object) -> str:
    """Preserve Learning String(20) facts exactly, including blank strings."""
    if key not in ENROLLMENT_INITIAL_STATE_POLICY_KEYS or type(value) is not str or len(value) > 20:
        raise ValueError(f"{key} must be a string of at most 20 characters")
    return value


ONBOARDING_POLICY_KEYS = ("onboarding_enrollment_global_role", "onboarding_task_catalog")


ONBOARDING_PROCEDURES = frozenset({"entrance_assessment", "confirm_details", "orientation"})


def validate_onboarding_policy_value(key: str, value: object) -> object:
    message = f"{key} has an invalid policy value"
    if key == "onboarding_enrollment_global_role":
        if type(value) is not str or not value.strip() or value != value.strip() or len(value) > 63:
            raise ValueError(message)
        return value
    if key != "onboarding_task_catalog":
        raise ValueError(message)
    if type(value) is str:
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            raise ValueError(message) from None
    if type(value) not in (list, tuple) or len(cast("list[object] | tuple[object, ...]", value)) != len(
        ONBOARDING_PROCEDURES
    ):
        raise ValueError(message)
    result = []
    for item in cast("list[object] | tuple[object, ...]", value):
        if type(item) not in (list, tuple) or len(cast("list[object] | tuple[object, ...]", item)) != 2:
            raise ValueError(message)
        key_name, label = cast("list[object] | tuple[object, ...]", item)
        if type(key_name) is not str or key_name not in ONBOARDING_PROCEDURES:
            raise ValueError(message)
        if type(label) is not str or not label.strip() or len(label) > 200:
            raise ValueError(message)
        result.append((key_name, label))
    if {item[0] for item in result} != ONBOARDING_PROCEDURES:
        raise ValueError(message)
    return tuple(result)


InvitationMemberRole = Literal["student", "instructor"]


def validate_invitation_default_member_role(value: object) -> InvitationMemberRole:
    """Accept only the owner's closed role contract, without coercion."""
    if type(value) is not str or value not in ("student", "instructor"):
        raise ValueError("invitation_default_member_role must be student or instructor")
    return cast(InvitationMemberRole, value)


def validate_invitation_member_role_map(value: object) -> dict[str, InvitationMemberRole]:
    """Parse a strict JSON object or mapping; never include input in errors."""
    message = "invitation_member_role_map must map nonempty role names to student or instructor"

    def unique_object(pairs):
        result = {}
        for key, role in pairs:
            if key in result:
                raise ValueError(message)
            result[key] = role
        return result

    if isinstance(value, str):
        try:
            value = json.loads(value, object_pairs_hook=unique_object)
        except (TypeError, ValueError):
            raise ValueError(message) from None
    if type(value) is not dict:
        raise ValueError(message)
    result: dict[str, InvitationMemberRole] = {}
    for key, role in value.items():
        if type(key) is not str or not key or key != key.strip():
            raise ValueError(message)
        try:
            result[key] = validate_invitation_default_member_role(role)
        except ValueError:
            raise ValueError(message) from None
    return result


def validate_web_account_creation_password_min_length(value: object) -> int:
    """Parse this entrypoint's nonnegative minimum without bool/float coercion."""
    if isinstance(value, str) and value.strip().isascii() and value.strip().isdecimal():
        value = int(value.strip())
    if type(value) is not int or value < 0:
        raise ValueError("web_account_creation_password_min_length must be a nonnegative integer")
    return value


def validate_web_account_creation_role_admission(value: object) -> dict[str, tuple[str, ...]]:
    """Validate configured role names and ordered, nonempty unique actor lists."""
    message = "web_account_creation_role_admission must map role names to nonempty unique actor-role lists"

    def unique_object(pairs):
        result = {}
        for key, actors in pairs:
            if key in result:
                raise ValueError(message)
            result[key] = actors
        return result

    if isinstance(value, str):
        try:
            value = json.loads(value, object_pairs_hook=unique_object)
        except (TypeError, ValueError):
            raise ValueError(message) from None
    if type(value) is not dict:
        raise ValueError(message)
    result: dict[str, tuple[str, ...]] = {}
    for target, actors in value.items():
        if type(target) is not str or not target or target != target.strip():
            raise ValueError(message)
        if type(actors) not in (list, tuple) or not actors:
            raise ValueError(message)
        if any(type(actor) is not str or not actor or actor != actor.strip() for actor in actors):
            raise ValueError(message)
        if len(set(actors)) != len(actors):
            raise ValueError(message)
        result[target] = tuple(actors)
    return result


def validate_web_account_creation_role_provisioning(value: object) -> dict[str, str]:
    """Validate role catalog creation names without inventing role defaults."""
    message = "web_account_creation_role_provisioning must map role names to nonempty display names"

    def unique_object(pairs):
        result = {}
        for key, name in pairs:
            if key in result:
                raise ValueError(message)
            result[key] = name
        return result

    if isinstance(value, str):
        try:
            value = json.loads(value, object_pairs_hook=unique_object)
        except (TypeError, ValueError):
            raise ValueError(message) from None
    if type(value) is not dict:
        raise ValueError(message)
    for role, name in value.items():
        if type(role) is not str or not role or role != role.strip():
            raise ValueError(message)
        if type(name) is not str or not name or name != name.strip():
            raise ValueError(message)
    return dict(value)


PASSWORD_CHANGE_POLICY_SHAPES = {"password_change_min_length": "nonnegative_int"}


def validate_password_change_policy_value(key: str, value: object) -> int:
    """Strict purpose minimum; zero is configurable and bool/float are refused."""
    if key not in PASSWORD_CHANGE_POLICY_SHAPES:
        raise ValueError("unknown password-change policy setting")
    message = f"{key} must be a nonnegative integer"
    if isinstance(value, str) and value.strip().isascii() and value.strip().isdecimal():
        try:
            value = int(value.strip())
        except ValueError:
            raise ValueError(message) from None
    if type(value) is not int or value < 0:
        raise ValueError(message)
    return value


INITIAL_IDENTITY_POLICY_KEYS = ("account_initial_is_active", "account_initial_profile_status")


def validate_initial_identity_policy_value(key: str, value: object) -> bool | str:
    """Parse exact creation settings without inventing account status policy."""
    message = f"{key} has an invalid policy value"
    if key == "account_initial_is_active":
        if type(value) is str:
            text = value.strip().lower()
            if text in {"true", "1", "yes", "on"}:
                return True
            if text in {"false", "0", "no", "off"}:
                return False
        if type(value) is not bool:
            raise ValueError(message)
        return value
    if key != "account_initial_profile_status" or type(value) is not str or len(value) > 20:
        raise ValueError(message)
    return value


PROFILE_POLICY_SHAPES = {
    "account_avatar_max_bytes": "positive_int",
    "account_email_results_default": "bool",
    "account_email_digest_default": "bool",
}


def validate_profile_policy_value(key: str, value: object) -> int | bool:
    """Strict stored/env profile policy; no malformed-value fallback."""
    message = f"{key} has an invalid policy value"
    if PROFILE_POLICY_SHAPES[key] == "positive_int":
        if isinstance(value, str) and value.strip().isascii() and value.strip().isdecimal():
            value = int(value.strip())
        # BinaryIO.read(limit + 1) accepts a Py_ssize_t count. Reserve the
        # extra overflow-detection byte; this is a platform representation
        # boundary, not an additional product/business upload limit.
        if type(value) is not int or not 0 < value < sys.maxsize:
            raise ValueError(message)
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "1", "yes", "on"}:
            return True
        if text in {"false", "0", "no", "off"}:
            return False
    if type(value) is not bool:
        raise ValueError(message)
    return value


AUTHENTICATION_POLICY_SHAPES = {
    "authentication_max_failed_logins": "positive_int",
    "authentication_lockout_minutes": "nonnegative_duration",
    "password_reset_token_minutes": "nonnegative_duration",
}


def validate_authentication_policy_value(key: str, value: object) -> int | float:
    """Strict product policy parsing; never clamp or echo the supplied value."""
    shape = AUTHENTICATION_POLICY_SHAPES[key]
    message = f"{key} has an invalid policy value"
    if isinstance(value, str):
        try:
            value = int(value.strip()) if shape == "positive_int" else float(value.strip())
        except (ValueError, OverflowError):
            raise ValueError(message) from None
    if shape == "positive_int":
        if type(value) is not int or value <= 0:
            raise ValueError(message)
        return value
    if type(value) not in (int, float) or cast(int | float, value) < 0:
        raise ValueError(message)
    try:
        normalized = float(cast(int | float, value))
        if not math.isfinite(normalized):
            raise ValueError(message)
        # Preserve native fractional-minute rounding. The representational
        # limit comes from timedelta itself, never a product business cap.
        timedelta(minutes=normalized)
    except (OverflowError, ValueError):
        raise ValueError(message) from None
    return normalized


BOOTSTRAP_POLICY_KEYS = (
    "bootstrap_allowed_database_role_pairs",
    "bootstrap_role_provisioning",
    "bootstrap_initial_role_slug",
    "bootstrap_tenant_is_active",
    "bootstrap_password_min_length",
    "bootstrap_owner_first_name",
    "bootstrap_owner_last_name_template",
)


def validate_bootstrap_policy_value(key: str, value: object) -> object:
    """Closed product composition; empty role pairs explicitly deny admission."""
    message = f"{key} has an invalid policy value"
    if key == "bootstrap_allowed_database_role_pairs":
        if type(value) is str:
            try:
                value = json.loads(value)
            except (ValueError, TypeError):
                raise ValueError(message) from None
        if not isinstance(value, (list, tuple)):
            raise ValueError(message)
        result = []
        for pair in value:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                raise ValueError(message)
            if any(type(role) is not str or not role or role != role.strip() or len(role) > 63 for role in pair):
                raise ValueError(message)
            result.append(tuple(pair))
        if len(set(result)) != len(result):
            raise ValueError(message)
        return tuple(result)
    if key == "bootstrap_role_provisioning":
        return validate_web_account_creation_role_provisioning(value)
    if key == "bootstrap_tenant_is_active":
        return validate_initial_identity_policy_value("account_initial_is_active", value)
    if key == "bootstrap_password_min_length":
        return validate_web_account_creation_password_min_length(value)
    if key == "bootstrap_owner_last_name_template":
        if type(value) is not str or value.count("{tenant_name}") != 1:
            raise ValueError(message)
        rest = value.replace("{tenant_name}", "")
        if "{" in rest or "}" in rest:
            raise ValueError(message)
        return value
    if key not in BOOTSTRAP_POLICY_KEYS or type(value) is not str or value != value.strip() or not value:
        raise ValueError(message)
    return value


def validate_password_reset_min_length(value: object) -> int:
    """Parse the reset setting without bool/float coercion or unsafe fallback."""
    if isinstance(value, str) and value.strip().isascii() and value.strip().isdecimal():
        value = int(value.strip())
    if type(value) is not int or value < 1:
        raise ValueError("password_reset_min_length must be a positive integer")
    return value


def validate_notifications_recent_limit(value: object) -> int:
    """Strict positive integer policy shared by environment and stored overrides."""
    message = "notifications_recent_limit must be a positive integer"
    if isinstance(value, str) and value.strip().isascii() and value.strip().isdecimal():
        try:
            value = int(value.strip())
        except ValueError:
            raise ValueError(message) from None
    if type(value) is not int or value < 1:
        raise ValueError(message)
    return value


ICAL_FEED_POLICY_KEYS = ("ical_feed_token_ttl_days", "ical_feed_uid_domain", "ical_feed_filename")


def validate_ical_feed_policy(key: str, value: object) -> int | str:
    """Validate environment/store values before rendering headers or minting."""
    if key == "ical_feed_token_ttl_days":
        if isinstance(value, str) and value.strip().isascii() and value.strip().isdecimal():
            value = int(value.strip())
        if type(value) is not int or value < 1:
            raise ValueError("ical_feed_token_ttl_days must be a positive integer")
        return value
    if type(value) is not str or not value.strip() or "\r" in value or "\n" in value:
        raise ValueError(f"{key} must be nonempty and contain no CR/LF")
    if key == "ical_feed_filename" and any(
        char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for char in value
    ):
        raise ValueError("ical_feed_filename must be a safe ASCII filename")
    return value


LEARNING_POLICY_SHAPES: dict[str, tuple[str, str]] = {
    "learning_reading_words_per_minute": ("int", "positive"),
    "learning_reading_minimum_minutes": ("int", "nonnegative"),
    "learning_join_lead_minutes": ("int", "nonnegative"),
    "learning_todo_graded_lookback_days": ("int", "nonnegative"),
    "learning_todo_graded_limit": ("int", "nonnegative"),
    "learning_todo_window_horizon_days": ("int", "nonnegative"),
    "learning_todo_near_deadline_days": ("int", "nonnegative"),
    "learning_todo_far_deadline_days": ("int", "nonnegative"),
    "learning_agenda_limit": ("int", "nonnegative"),
    "learning_person_announcements_limit": ("int", "nonnegative"),
    "learning_tenant_announcements_limit": ("int", "nonnegative"),
    "learning_dashboard_at_risk_fraction": ("float", "fraction"),
    "learning_dashboard_stale_days": ("int", "nonnegative"),
    "learning_behind_pace_fraction": ("float", "fraction"),
    "learning_search_limit": ("int", "nonnegative"),
    "learning_search_staff_roles": ("tuple[str, ...]", "role_slugs"),
    "learning_top_movers_limit": ("int", "nonnegative"),
    "learning_attention_limit": ("int", "nonnegative"),
    "learning_activity_report_hours": ("int", "nonnegative"),
    "learning_lab_pass_threshold": ("float", "fraction"),
    "learning_instructor_at_risk_fraction": ("float", "fraction"),
    "learning_instructor_stale_days": ("int", "nonnegative"),
    "learning_instructor_workspace_limit": ("int", "nonnegative"),
    "learning_trend_sample_limit": ("int", "at_least_two"),
    "learning_trend_min_samples": ("int", "at_least_two"),
    "learning_trend_min_delta_fraction": ("float", "fraction"),
    "learning_activity_chart_weeks": ("int", "nonnegative"),
    "learning_velocity_weeks": ("int", "nonnegative"),
    "learning_resume_after_days": ("int", "nonnegative"),
    "learning_active_window_days": ("int", "nonnegative"),
    "learning_comparison_prior_weeks": ("int", "positive"),
    "success_inactive_days": ("int", "signed"),
    "success_min_grade_pct": ("int", "signed"),
    "success_certificate_grace_hours": ("int", "signed"),
    "success_inactivity_high_factor": ("float", "at_least_one"),
    "success_overdue_high_count": ("int", "nonnegative"),
    "success_overdue_high_days": ("int", "nonnegative"),
    "success_overdue_title_limit": ("int", "nonnegative"),
    "success_below_passing_min_graded": ("int", "nonnegative"),
    "success_below_passing_min_weight_fraction": ("float", "fraction"),
    "success_below_passing_high_grade_factor": ("float", "fraction"),
    "success_almost_complete_fraction": ("float", "fraction"),
    "learning_default_activity_type": ("str", "text"),
    "learning_default_activity_pass_threshold": ("float", "fraction"),
    "learning_default_activity_assessment_mode": ("str", "text"),
    "learning_default_activity_grading": ("str", "text"),
    "learning_default_activity_weight": ("float", "positive"),
    "learning_default_course_status": ("str", "text"),
    "learning_default_course_listed": ("bool", "bool"),
    "learning_default_session_type": ("str", "session_type"),
    "learning_default_extra_attempts": ("int", "positive"),
    "learning_bank_pass_threshold_by_kind": ("dict[str, float]", "fraction_map"),
    "learning_bank_fallback_pass_threshold": ("float", "fraction"),
    "learning_bank_new_activity_assessment_mode": ("str", "text"),
    "learning_bank_new_activity_grading": ("str", "text"),
    "learning_bank_new_activity_weight": ("float", "positive"),
    "learning_exam_seconds_per_question": ("float", "nonnegative"),
    "learning_exam_near_chance_margin": ("float", "positive"),
    "learning_exam_fallback_score_floor": ("float", "fraction"),
    "learning_exam_fallback_duration_seconds": ("float", "nonnegative"),
    "learning_missing_question_weight": ("int", "positive"),
    # Composable Learning policies (A1): creation defaults and Terms inputs.
    # Defaults in Settings reproduce the historical hardcoded behaviour.
    "learning_prerequisite_default_kind": ("str", "prerequisite_kind"),
    "learning_prerequisite_default_composition": ("str", "prerequisite_composition"),
    "learning_track_default_gating_mode": ("str", "track_gating_mode"),
    "learning_default_completion_fraction": ("float", "positive_fraction"),
    "learning_default_certificate_auto_issue": ("bool", "bool"),
    "learning_default_certificate_validity_days": ("int", "nonnegative"),
    "learning_feedback_reveal_by_mode": ("dict[str, str]", "reveal_map"),
    "learning_default_activity_scoring_policy": ("str", "scoring_policy"),
    "learning_progress_trend_sample_limit": ("int", "at_least_two"),
    "learning_progress_trend_min_samples": ("int", "at_least_two"),
    "learning_progress_trend_min_delta_fraction": ("float", "fraction"),
    "learning_cohort_half_fraction": ("float", "fraction"),
    "success_enabled_rules": ("tuple[str, ...]", "success_rules"),
    "success_severity_by_rule": ("dict[str, str]", "severity_map"),
    "learning_session_promotes_delivery_mode": ("bool", "bool"),
    "learning_default_attempt_cooldown_seconds": ("int", "cooldown_seconds"),
    "learning_pacing_default_open": ("bool", "bool"),
}


def validate_admin_home_access_roles(value: object) -> tuple[str, ...]:
    """Custom roles allowed, empty denies, malformed policy refuses."""
    message = "admin_home_access_roles has an invalid policy value"
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            raise ValueError(message) from None
    if type(value) not in (list, tuple):
        raise ValueError(message)
    value = cast("list[object] | tuple[object, ...]", value)
    if any(type(slug) is not str or not slug or slug != slug.strip() for slug in value):
        raise ValueError(message)
    if len(set(value)) != len(value):
        raise ValueError(message)
    return tuple(cast("list[str] | tuple[str, ...]", value))


def validate_lab_admin_access_roles(value: object) -> tuple[str, ...]:
    """Custom roles allowed, empty denies, malformed policy refuses."""
    message = "lab_admin_access_roles has an invalid policy value"
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            raise ValueError(message) from None
    if type(value) not in (list, tuple):
        raise ValueError(message)
    value = cast("list[object] | tuple[object, ...]", value)
    if any(type(slug) is not str or not slug or slug != slug.strip() for slug in value):
        raise ValueError(message)
    if len(set(value)) != len(value):
        raise ValueError(message)
    return tuple(cast("list[str] | tuple[str, ...]", value))


def validate_reminders_admin_access_roles(value: object) -> tuple[str, ...]:
    """Custom roles allowed, empty denies, malformed policy refuses."""
    message = "reminders_admin_access_roles has an invalid policy value"
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            raise ValueError(message) from None
    if type(value) not in (list, tuple):
        raise ValueError(message)
    value = cast("list[object] | tuple[object, ...]", value)
    if any(type(slug) is not str or not slug or slug != slug.strip() for slug in value):
        raise ValueError(message)
    if len(set(value)) != len(value):
        raise ValueError(message)
    return tuple(cast("list[str] | tuple[str, ...]", value))


def validate_teaching_access_roles(value: object) -> tuple[str, ...]:
    """Explicit role policy: custom slugs allowed, empty denies, malformed refuses."""
    message = "teaching_access_roles has an invalid policy value"
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            raise ValueError(message) from None
    if type(value) not in (list, tuple):
        raise ValueError(message)
    value = cast("list[object] | tuple[object, ...]", value)
    if any(type(slug) is not str or not slug or slug != slug.strip() for slug in value):
        raise ValueError(message)
    if len(set(value)) != len(value):
        raise ValueError(message)
    return tuple(cast("list[str] | tuple[str, ...]", value))


def validate_catalog_staff_roles(value: object) -> tuple[str, ...]:
    """Explicit role policy: custom slugs allowed, empty denies, malformed refuses."""
    message = "catalog_staff_roles has an invalid policy value"
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            raise ValueError(message) from None
    if type(value) not in (list, tuple):
        raise ValueError(message)
    value = cast("list[object] | tuple[object, ...]", value)
    if any(type(slug) is not str or not slug or slug != slug.strip() for slug in value):
        raise ValueError(message)
    if len(set(value)) != len(value):
        raise ValueError(message)
    return tuple(cast("list[str] | tuple[str, ...]", value))


def validate_gradebook_access_roles(value: object) -> tuple[str, ...]:
    """Explicit role policy: custom slugs allowed, empty denies, malformed refuses."""
    message = "gradebook_access_roles has an invalid policy value"
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            raise ValueError(message) from None
    if type(value) not in (list, tuple):
        raise ValueError(message)
    value = cast("list[object] | tuple[object, ...]", value)
    if any(type(slug) is not str or not slug or slug != slug.strip() for slug in value):
        raise ValueError(message)
    if len(set(value)) != len(value):
        raise ValueError(message)
    return tuple(cast("list[str] | tuple[str, ...]", value))


def validate_settings_access_roles(value: object) -> tuple[str, ...]:
    """Explicit role policy: custom slugs allowed, empty denies, malformed refuses."""
    message = "settings_access_roles has an invalid policy value"
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            raise ValueError(message) from None
    if type(value) not in (list, tuple):
        raise ValueError(message)
    value = cast("list[object] | tuple[object, ...]", value)
    if any(type(slug) is not str or not slug or slug != slug.strip() for slug in value):
        raise ValueError(message)
    if len(set(value)) != len(value):
        raise ValueError(message)
    return tuple(cast("list[str] | tuple[str, ...]", value))


def validate_reports_access_roles(value: object) -> tuple[str, ...]:
    """Explicit role policy: custom slugs allowed, empty denies, malformed refuses."""
    message = "reports_access_roles has an invalid policy value"
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            raise ValueError(message) from None
    if type(value) not in (list, tuple):
        raise ValueError(message)
    value = cast("list[object] | tuple[object, ...]", value)
    if any(type(slug) is not str or not slug or slug != slug.strip() for slug in value):
        raise ValueError(message)
    if len(set(value)) != len(value):
        raise ValueError(message)
    return tuple(cast("list[str] | tuple[str, ...]", value))


def validate_bank_author_roles(value: object) -> tuple[str, ...]:
    return _validate_bank_role_list("bank_author_roles", value)


def validate_bank_publish_roles(value: object) -> tuple[str, ...]:
    return _validate_bank_role_list("bank_publish_roles", value)


def validate_bank_repoint_roles(value: object) -> tuple[str, ...]:
    return _validate_bank_role_list("bank_repoint_roles", value)


def validate_bank_export_roles(value: object) -> tuple[str, ...]:
    return _validate_bank_role_list("bank_export_roles", value)


def validate_bank_retire_roles(value: object) -> tuple[str, ...]:
    return _validate_bank_role_list("bank_retire_roles", value)


def validate_learning_policy_value(key: str, value: object) -> object:
    """Reject malformed explicit policy rather than select a silent fallback."""
    kind, shape = LEARNING_POLICY_SHAPES[key]
    message = f"{key} has an invalid policy value"
    if kind == "tuple[str, ...]":
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                raise ValueError(message) from None
        if type(value) not in (list, tuple):
            raise ValueError(message)
        value = cast("list[object] | tuple[object, ...]", value)
        if any(type(slug) is not str or not slug or slug != slug.strip() for slug in value):
            raise ValueError(message)
        if len(set(value)) != len(value):
            raise ValueError(message)
        if shape == "success_rules":
            from dotmac_learning.delivery_models import REASON_KINDS

            if not set(value) <= set(REASON_KINDS):
                raise ValueError(message)
        return tuple(cast("list[str] | tuple[str, ...]", value))
    if kind == "dict[str, str]":
        if isinstance(value, str):

            def unique_str_object(pairs):
                result = {}
                for name, item in pairs:
                    if name in result:
                        raise ValueError(message)
                    result[name] = item
                return result

            try:
                value = json.loads(value, object_pairs_hook=unique_str_object)
            except (TypeError, ValueError):
                raise ValueError(message) from None
        if type(value) is not dict:
            raise ValueError(message)
        result_str: dict[str, str] = {}
        for name, item in value.items():
            if type(name) is not str or not name or name != name.strip():
                raise ValueError(message)
            if type(item) is not str or not item or item != item.strip():
                raise ValueError(message)
            result_str[name] = item
        if shape == "reveal_map":
            from dotmac_learning import ASSESSMENT_MODES

            if set(result_str) != set(ASSESSMENT_MODES):
                raise ValueError(message)
            if any(item not in LEARNING_FEEDBACK_REVEAL_POLICIES for item in result_str.values()):
                raise ValueError(message)
            if result_str.get("exam") == "immediate":
                raise ValueError(message)
        elif shape == "severity_map":
            from dotmac_learning.delivery_models import REASON_KINDS

            if not set(result_str) <= set(REASON_KINDS):
                raise ValueError(message)
            if any(item not in LEARNING_SUCCESS_SEVERITIES for item in result_str.values()):
                raise ValueError(message)
        return result_str
    if kind == "dict[str, float]":
        if isinstance(value, str):

            def unique_object(pairs):
                result = {}
                for name, threshold in pairs:
                    if name in result:
                        raise ValueError(message)
                    result[name] = threshold
                return result

            try:
                value = json.loads(value, object_pairs_hook=unique_object)
            except (TypeError, ValueError):
                raise ValueError(message) from None
        if type(value) is not dict:
            raise ValueError(message)
        result = {}
        for name, threshold in value.items():
            if type(name) is not str or not name or name != name.strip():
                raise ValueError(message)
            if type(threshold) not in (int, float) or not math.isfinite(threshold) or not 0 <= threshold <= 1:
                raise ValueError(message)
            result[name] = float(threshold)
        return result
    if kind == "str":
        if type(value) is not str or not value or value != value.strip():
            raise ValueError(message)
        if shape == "session_type":
            from dotmac_learning import SESSION_TYPES

            if value not in SESSION_TYPES:
                raise ValueError(message)
        elif shape == "prerequisite_kind":
            from dotmac_learning import PREREQUISITE_CONDITION_KINDS

            if value not in PREREQUISITE_CONDITION_KINDS:
                raise ValueError(message)
        elif shape == "prerequisite_composition":
            if value not in LEARNING_PREREQUISITE_COMPOSITIONS:
                raise ValueError(message)
        elif shape == "track_gating_mode":
            if value not in LEARNING_TRACK_GATING_MODES:
                raise ValueError(message)
        elif shape == "scoring_policy":
            if value not in LEARNING_SCORING_POLICIES:
                raise ValueError(message)
        return value
    if kind == "bool":
        if isinstance(value, str):
            parsed = value.strip().lower()
            if parsed in ("true", "1", "yes", "on"):
                return True
            if parsed in ("false", "0", "no", "off"):
                return False
        if type(value) is not bool:
            raise ValueError(message)
        return value
    if isinstance(value, str):
        try:
            value = int(value.strip()) if kind == "int" else float(value.strip())
        except (ValueError, OverflowError):
            raise ValueError(message) from None
    if kind == "int":
        if type(value) is not int:
            raise ValueError(message)
    elif type(value) not in (int, float) or not math.isfinite(cast(int | float, value)):
        raise ValueError(message)
    value = cast(int | float, value)
    if shape == "nonnegative" and value < 0:
        raise ValueError(message)
    if shape == "positive" and value <= 0:
        raise ValueError(message)
    if shape == "at_least_two" and value < 2:
        raise ValueError(message)
    if shape == "at_least_one" and value < 1:
        raise ValueError(message)
    if shape == "fraction" and not 0 <= value <= 1:
        raise ValueError(message)
    if shape == "positive_fraction" and not 0 < value <= 1:
        raise ValueError(message)
    if shape == "cooldown_seconds" and not 0 <= value <= LEARNING_ATTEMPT_COOLDOWN_MAX_SECONDS:
        raise ValueError(message)
    return value if kind == "int" else float(value)


def validate_learning_policy_relations(values: dict[str, object]) -> None:
    """Reject contradictory composed policy without clamping the input."""
    if cast(int, values["learning_todo_near_deadline_days"]) > cast(int, values["learning_todo_far_deadline_days"]):
        raise ValueError("near deadline days must not exceed far deadline days")
    if cast(int, values["learning_trend_sample_limit"]) < cast(int, values["learning_trend_min_samples"]):
        raise ValueError("trend sample limit must cover minimum samples")
    if cast(int, values["learning_progress_trend_sample_limit"]) < cast(
        int, values["learning_progress_trend_min_samples"]
    ):
        raise ValueError("progress trend sample limit must cover minimum samples")


LEARNING_PREREQUISITE_COMPOSITIONS = ("all", "any")


LEARNING_TRACK_GATING_MODES = ("none", "previous", "all_previous")


LEARNING_SCORING_POLICIES = ("best", "latest", "first")


LEARNING_FEEDBACK_REVEAL_POLICIES = ("immediate", "on_pass_or_exhausted", "never")


LEARNING_SUCCESS_SEVERITIES = ("high", "medium", "low")


LEARNING_ATTEMPT_COOLDOWN_MAX_SECONDS = 2592000


def _validate_bank_role_list(key: str, value: object) -> tuple[str, ...]:
    """Explicit role policy: custom slugs allowed, empty denies, malformed refuses."""
    message = f"{key} has an invalid policy value"
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            raise ValueError(message) from None
    if type(value) not in (list, tuple):
        raise ValueError(message)
    value = cast("list[object] | tuple[object, ...]", value)
    if any(type(slug) is not str or not slug or slug != slug.strip() for slug in value):
        raise ValueError(message)
    if len(set(value)) != len(value):
        raise ValueError(message)
    return tuple(cast("list[str] | tuple[str, ...]", value))
