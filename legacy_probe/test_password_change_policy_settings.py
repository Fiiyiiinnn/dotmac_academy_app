"""Password-change purpose settings canaries; source authored, not locally run.

These configuration-only tests never claim credential-owner, route, confirmation,
proof, database/RLS, or integration coverage. They preserve creation/reset policy
independence while qualifying the future adapter's resolved min_length input.
"""

from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from app.config import Settings, validate_password_change_policy_value
from app.services import password_change_policy as policy
from app.services import settings_store

KEY = "password_change_min_length"


def test_default_belongs_to_settings_and_is_separate_from_creation_and_reset(monkeypatch):
    for key in ("PASSWORD_CHANGE_MIN_LENGTH", "WEB_ACCOUNT_CREATION_PASSWORD_MIN_LENGTH", "PASSWORD_RESET_MIN_LENGTH"):
        monkeypatch.delenv(key, raising=False)
    configured = Settings(_env_file=None)
    assert configured.password_change_min_length == 8
    assert configured.web_account_creation_password_min_length == 0
    assert configured.password_reset_min_length == 8
    with pytest.raises(TypeError):
        policy.PasswordChangePolicy()  # A purpose-policy object has no hidden default.


@pytest.mark.parametrize(
    "value", [None, True, False, -1, 0.0, 1.0, 2.5, "-1", "+1", "1.0", "true", "", [], {}, chr(0xFF11) + chr(0xFF12)]
)
def test_malformed_bool_null_and_negative_setting_refuse(value):
    with pytest.raises(ValueError, match="password_change_min_length must be a nonnegative integer"):
        validate_password_change_policy_value(KEY, value)
    with pytest.raises(ValueError):
        Settings(_env_file=None, password_change_min_length=value)


@pytest.mark.parametrize(
    "raw,expected", [(0, 0), (1, 1), ("0", 0), (" 12 ", 12), (10**400, 10**400), (str(10**400), 10**400)]
)
def test_nonnegative_integers_include_zero_and_huge_comparison_safe_values(raw, expected):
    assert validate_password_change_policy_value(KEY, raw) == expected
    configured = Settings(_env_file=None, password_change_min_length=raw)
    assert configured.password_change_min_length == expected
    assert type(configured.password_change_min_length) is int


@pytest.mark.parametrize("raw,expected", [("0", 0), ("17", 17), (str(10**400), 10**400)])
def test_environment_sets_only_the_change_purpose(monkeypatch, raw, expected):
    monkeypatch.setenv("PASSWORD_CHANGE_MIN_LENGTH", raw)
    monkeypatch.setenv("PASSWORD_RESET_MIN_LENGTH", "9")
    monkeypatch.setenv("WEB_ACCOUNT_CREATION_PASSWORD_MIN_LENGTH", "2")
    configured = Settings(_env_file=None)
    assert configured.password_change_min_length == expected
    assert configured.password_reset_min_length == 9
    assert configured.web_account_creation_password_min_length == 2


@pytest.mark.parametrize("raw", ["-1", "false", "1.5", ""])
def test_malformed_environment_refuses_without_default(monkeypatch, raw):
    monkeypatch.setenv("PASSWORD_CHANGE_MIN_LENGTH", raw)
    with pytest.raises(ValueError):
        Settings(_env_file=None)


def test_factory_uses_actual_session_and_requires_store(monkeypatch):
    db, calls = object(), []

    def effective(session, *, require_store):
        calls.append((session, require_store))
        return SimpleNamespace(password_change_min_length=17)

    monkeypatch.setattr(settings_store, "effective", effective)
    resolved = policy.password_change_policy(db)
    assert calls == [(db, True)]
    assert resolved.min_length == 17
    assert not hasattr(resolved, "secret") and not hasattr(resolved, "password")
    assert not hasattr(resolved, "validate") and not hasattr(resolved, "authorize")
    with pytest.raises(FrozenInstanceError):
        resolved.min_length = 0


@pytest.mark.parametrize("minimum", [0, 13, 10**400])
def test_database_override_wins_over_live_environment_settings(monkeypatch, minimum):
    monkeypatch.setattr(settings_store.settings, KEY, 23)
    monkeypatch.setattr(settings_store, "get_all", lambda db: {KEY: str(minimum)})
    resolved = policy.password_change_policy(object())
    assert resolved.min_length == minimum
    assert type(resolved.min_length) is int


def test_empty_store_uses_live_settings_and_preserves_existing_effective_values(monkeypatch):
    before = settings_store._defaults()
    monkeypatch.setattr(settings_store.settings, KEY, 29)
    monkeypatch.setattr(settings_store, "get_all", lambda db: {})
    effective = settings_store.effective(object(), require_store=True)
    assert effective.password_change_min_length == 29
    assert policy.password_change_policy(object()).min_length == 29
    assert {key: value for key, value in effective.__dict__.items() if key != KEY} == {
        key: value for key, value in before.items() if key != KEY
    }


@pytest.mark.parametrize("raw", [None, "-1", "false", "1.5", "", "malformed-policy-marker"])
def test_bad_stored_policy_never_falls_back_or_echoes_value(monkeypatch, raw):
    monkeypatch.setattr(settings_store.settings, KEY, 11)
    monkeypatch.setattr(settings_store, "get_all", lambda db: {KEY: raw})
    with pytest.raises(ValueError) as caught:
        policy.password_change_policy(object())
    assert str(caught.value) == "password_change_min_length must be a nonnegative integer"


def test_get_all_preserves_stored_null_for_change_refusal_without_changing_legacy_null_omission():
    rows = [SimpleNamespace(key=KEY, value=None), SimpleNamespace(key="smtp_host", value=None)]
    db = SimpleNamespace(execute=lambda statement: SimpleNamespace(all=lambda: rows))
    assert settings_store.get_all(db) == {KEY: None}


@pytest.mark.parametrize("raw", [None, True, False, "-1", "false", "1.0", ""])
def test_invalid_settings_write_refuses_before_any_database_mutation(raw):
    with pytest.raises(ValueError):
        settings_store.set_many(object(), {KEY: raw})


def test_missing_database_and_store_read_failure_refuse(monkeypatch):
    with pytest.raises(ValueError, match="settings database session"):
        policy.password_change_policy(None)
    failure = RuntimeError("settings store unavailable")

    def broken(db):
        raise failure

    monkeypatch.setattr(settings_store, "get_all", broken)
    with pytest.raises(RuntimeError) as caught:
        policy.password_change_policy(object())
    assert caught.value is failure


@pytest.mark.parametrize("value", [None, True, False, -1, 0.0, "0", "12"])
def test_explicit_policy_object_requires_strict_integer_and_does_not_coerce(value):
    with pytest.raises(ValueError):
        policy.PasswordChangePolicy(min_length=value)


def test_explicit_policy_object_has_only_resolved_minimum():
    resolved = policy.PasswordChangePolicy(min_length=0)
    assert resolved.min_length == 0
    assert resolved.__slots__ == ("min_length",)
