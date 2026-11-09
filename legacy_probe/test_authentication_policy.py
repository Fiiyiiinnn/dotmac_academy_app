"""Strict policy composition boundaries; authored before implementation."""

import pytest

from app.config import Settings, validate_authentication_policy_value
from app.services import settings_store
from app.services.authentication_policy import authentication_policy


@pytest.mark.parametrize(
    "key,value",
    [
        ("authentication_max_failed_logins", 0),
        ("authentication_max_failed_logins", True),
        ("authentication_max_failed_logins", 1.2),
        ("authentication_max_failed_logins", None),
        ("authentication_lockout_minutes", -1),
        ("authentication_lockout_minutes", float("inf")),
        ("authentication_lockout_minutes", float("nan")),
        ("authentication_lockout_minutes", None),
    ],
)
def test_invalid_policy_refuses(key, value):
    with pytest.raises(ValueError):
        validate_authentication_policy_value(key, value)


def test_defaults_only_in_settings_and_nondefault_composition(monkeypatch):
    defaults = Settings(_env_file=None)
    assert defaults.authentication_max_failed_logins == 5
    assert defaults.authentication_lockout_minutes == 15

    def effective(db, *, require_store):
        assert require_store is True
        return settings_store.EffectiveSettings(
            {"authentication_max_failed_logins": 2, "authentication_lockout_minutes": 0}
        )

    monkeypatch.setattr(settings_store, "effective", effective)
    policy = authentication_policy(object())
    assert policy.maximum_failures == 2 and policy.lockout_minutes == 0


def test_store_failure_is_not_env_fallback(monkeypatch):
    def fail(db, *, require_store):
        assert require_store
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(settings_store, "effective", fail)
    with pytest.raises(RuntimeError, match="store unavailable"):
        authentication_policy(object())


@pytest.mark.parametrize("key", ["authentication_max_failed_logins", "authentication_lockout_minutes"])
def test_stored_null_and_malformed_policy_never_fall_back(key):
    for raw in (None, "invalid"):
        with pytest.raises(ValueError):
            settings_store._coerce(key, raw, 123)


def test_strict_composition_requires_database_session():
    with pytest.raises(ValueError, match="database session"):
        settings_store.effective(None, require_store=True)
