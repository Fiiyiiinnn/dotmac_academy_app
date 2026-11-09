"""Strict invitation purposes; authored NOT RUN, configuration-only coverage."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.config import INVITATION_PURPOSE_POLICY_SHAPES, Settings, validate_invitation_purpose_policy_value
from app.services import settings_store
from app.services.invitation_policy import invitation_acceptance_policy, invitation_issuance_policy


def test_settings_are_only_compatibility_defaults(monkeypatch):
    for key in INVITATION_PURPOSE_POLICY_SHAPES:
        monkeypatch.delenv(key.upper(), raising=False)
    configured = Settings(_env_file=None)
    assert configured.invitation_token_hours == 168
    assert configured.invitation_password_min_length == 8
    assert configured.invitation_human_replace_outstanding is True
    assert configured.invitation_stranded_replace_outstanding is False
    assert configured.web_account_creation_password_min_length == 0


@pytest.mark.parametrize("hours", [0, -1, 0.25, "0", "-1", "0.25"])
def test_duration_preserves_representable_native_signed_fractional_and_zero_values(hours):
    value = validate_invitation_purpose_policy_value("invitation_token_hours", hours)
    assert timedelta(hours=value) == timedelta(hours=float(hours))


@pytest.mark.parametrize(
    "value", [None, True, False, "", "nan", "inf", float("nan"), float("inf"), 10**400, "malformed"]
)
def test_invalid_duration_refuses_without_arbitrary_clamp(value):
    with pytest.raises(ValueError):
        validate_invitation_purpose_policy_value("invitation_token_hours", value)


@pytest.mark.parametrize("value", [None, True, False, 0, -1, 1.0, "", "0", "1.5"])
def test_acceptance_minimum_refuses_malformed_nonpositive_values(value):
    with pytest.raises(ValueError):
        validate_invitation_purpose_policy_value("invitation_password_min_length", value)


@pytest.mark.parametrize("key", ["invitation_human_replace_outstanding", "invitation_stranded_replace_outstanding"])
@pytest.mark.parametrize("value", [None, 1, 0, "", "maybe", [], {}])
def test_invalidation_policy_is_strict_bool_not_truthiness(key, value):
    with pytest.raises(ValueError):
        validate_invitation_purpose_policy_value(key, value)


def test_database_over_environment_and_purpose_separation(monkeypatch):
    monkeypatch.setattr(settings_store.settings, "invitation_token_hours", 2)
    monkeypatch.setattr(
        settings_store,
        "get_all",
        lambda db: {
            "invitation_token_hours": "0.25",
            "invitation_human_replace_outstanding": "false",
            "invitation_stranded_replace_outstanding": "true",
            "invitation_password_min_length": "13",
            "password_reset_min_length": "9",
            "password_change_min_length": "10",
            "web_account_creation_password_min_length": "0",
        },
    )
    human = invitation_issuance_policy(object(), purpose="human")
    stranded = invitation_issuance_policy(object(), purpose="stranded")
    assert human.token_hours == stranded.token_hours == 0.25
    assert human.replace_outstanding is False and stranded.replace_outstanding is True
    assert invitation_acceptance_policy(object()).minimum_length == 13
    now = datetime.now(UTC)
    assert human.deadline(now, ttl_hours=0) == now
    assert human.deadline(now, ttl_hours=-1) == now - timedelta(hours=1)
    assert human.deadline(now) == now + timedelta(hours=0.25)


def test_environment_is_read_by_settings_and_empty_store_uses_live_defaults(monkeypatch):
    monkeypatch.setenv("INVITATION_TOKEN_HOURS", "-0.5")
    monkeypatch.setenv("INVITATION_PASSWORD_MIN_LENGTH", "11")
    configured = Settings(_env_file=None)
    assert configured.invitation_token_hours == -0.5 and configured.invitation_password_min_length == 11
    monkeypatch.setattr(settings_store.settings, "invitation_token_hours", configured.invitation_token_hours)
    monkeypatch.setattr(settings_store, "get_all", lambda db: {})
    assert invitation_issuance_policy(object(), purpose="human").token_hours == -0.5


@pytest.mark.parametrize("key", list(INVITATION_PURPOSE_POLICY_SHAPES))
def test_stored_null_is_retained_and_refused_before_setting_writes(monkeypatch, key):
    rows = [SimpleNamespace(key=key, value=None)]
    db = SimpleNamespace(execute=lambda statement: SimpleNamespace(all=lambda: rows))
    assert settings_store.get_all(db) == {key: None}
    monkeypatch.setattr(settings_store, "get_all", lambda session: {key: None})
    with pytest.raises(ValueError):
        invitation_issuance_policy(object(), purpose="human")
    with pytest.raises(ValueError):
        settings_store.set_many(object(), {key: None})


def test_missing_store_unreadable_store_and_datetime_range_refuse(monkeypatch):
    with pytest.raises(ValueError):
        invitation_issuance_policy(None, purpose="human")

    def unavailable(db):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(settings_store, "get_all", unavailable)
    with pytest.raises(RuntimeError):
        invitation_issuance_policy(object(), purpose="human")
    monkeypatch.setattr(settings_store, "get_all", lambda db: {})
    policy = invitation_issuance_policy(object(), purpose="human")
    with pytest.raises(ValueError, match="expiry is not representable"):
        policy.deadline(datetime.max.replace(tzinfo=UTC), ttl_hours=1)
    with pytest.raises(ValueError, match="expiry is not representable"):
        policy.deadline(datetime.min.replace(tzinfo=UTC), ttl_hours=-1)
    with pytest.raises(ValueError):
        invitation_issuance_policy(object(), purpose="caller-controlled-unknown")
