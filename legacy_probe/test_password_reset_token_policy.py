"""Focused configuration seam checks; authored only, no local runtime execution."""

from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.services import settings_store
from app.services.credential_policy import password_reset_token_policy


def test_legacy_two_hour_default_is_configuration_and_environment_can_override(monkeypatch):
    monkeypatch.delenv("PASSWORD_RESET_TOKEN_MINUTES", raising=False)
    assert Settings(_env_file=None).password_reset_token_minutes == 120
    monkeypatch.setenv("PASSWORD_RESET_TOKEN_MINUTES", "0.5")
    assert Settings(_env_file=None).password_reset_token_minutes == 0.5


def test_database_override_uses_required_strict_store_and_exact_session(monkeypatch):
    monkeypatch.setattr(settings_store.settings, "password_reset_token_minutes", 120)
    db = Mock()
    reader = Mock(return_value={"password_reset_token_minutes": "0.125"})
    monkeypatch.setattr(settings_store, "get_all", reader)
    policy = password_reset_token_policy(db)
    assert policy.minutes == 0.125
    reader.assert_called_once_with(db)
    assert "password_reset_token_minutes" in settings_store.KNOWN_KEYS


@pytest.mark.parametrize("value", [True, False, None, -1, "bad", "nan", "inf", "1e300"])
def test_invalid_configuration_and_stored_override_refuse(monkeypatch, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, password_reset_token_minutes=value)
    monkeypatch.setattr(settings_store, "get_all", lambda db: {"password_reset_token_minutes": value})
    with pytest.raises(ValueError, match="password_reset_token_minutes"):
        password_reset_token_policy(Mock())


def test_required_store_failure_and_missing_session_never_fall_back(monkeypatch):
    reader = Mock(side_effect=RuntimeError("settings unavailable"))
    monkeypatch.setattr(settings_store, "get_all", reader)
    with pytest.raises(RuntimeError, match="settings unavailable"):
        password_reset_token_policy(Mock())
    with pytest.raises(ValueError, match="session"):
        password_reset_token_policy(None)
