"""Strict initial-state policy canaries; authored for hosted execution only."""

from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from app.config import Settings, validate_initial_identity_policy_value
from app.services import initial_identity_policy as policy
from app.services import settings_store


@pytest.mark.parametrize(
    "raw,expected", [(True, True), (False, False), ("true", True), ("false", False), ("0", False), ("off", False)]
)
def test_boolean_parsing_distinguishes_explicit_false(raw, expected):
    assert validate_initial_identity_policy_value("account_initial_is_active", raw) is expected


@pytest.mark.parametrize("bad", [None, 0, 1, 0.0, [], {}, "", "null", "malformed"])
def test_boolean_refuses_coercive_or_null_values(bad):
    with pytest.raises(ValueError):
        validate_initial_identity_policy_value("account_initial_is_active", bad)
    with pytest.raises(ValueError):
        Settings(_env_file=None, account_initial_is_active=bad)


@pytest.mark.parametrize("status", ["", "   ", " suspended ", "é" * 20, "x" * 20])
def test_status_preserves_exact_storage_permitted_content(status):
    configured = Settings(_env_file=None, account_initial_profile_status=status)
    assert configured.account_initial_profile_status == status
    assert policy.InitialIdentityPolicy(False, status).profile_status == status


@pytest.mark.parametrize("bad", [None, True, 1, [], "x" * 21, "é" * 21])
def test_status_refuses_wrong_type_and_storage_overflow(bad):
    with pytest.raises(ValueError):
        Settings(_env_file=None, account_initial_profile_status=bad)
    with pytest.raises(ValueError):
        policy.InitialIdentityPolicy(False, bad)


def test_defaults_environment_and_required_immutable_arguments(monkeypatch):
    configured = Settings(_env_file=None)
    assert configured.account_initial_is_active is True
    assert configured.account_initial_profile_status == "active"
    monkeypatch.setenv("ACCOUNT_INITIAL_IS_ACTIVE", "false")
    monkeypatch.setenv("ACCOUNT_INITIAL_PROFILE_STATUS", " suspended ")
    configured = Settings(_env_file=None)
    assert configured.account_initial_is_active is False
    assert configured.account_initial_profile_status == " suspended "
    with pytest.raises(TypeError):
        policy.InitialIdentityPolicy()
    value = policy.InitialIdentityPolicy(False, "suspended")
    with pytest.raises(FrozenInstanceError):
        value.is_active = True
    with pytest.raises(ValueError):
        policy.InitialIdentityPolicy("false", "active")


def test_database_precedence_missing_fallback_and_pinned_decision(monkeypatch):
    monkeypatch.setattr(settings_store.settings, "account_initial_is_active", True)
    monkeypatch.setattr(settings_store.settings, "account_initial_profile_status", "active")
    rows = {"account_initial_is_active": "false", "account_initial_profile_status": "suspended"}
    monkeypatch.setattr(settings_store, "get_all", lambda db: rows)
    first = policy.initial_identity_policy(object())
    assert first == policy.InitialIdentityPolicy(False, "suspended")
    rows.clear()
    assert policy.initial_identity_policy(object()) == policy.InitialIdentityPolicy(True, "active")
    assert first == policy.InitialIdentityPolicy(False, "suspended")


@pytest.mark.parametrize(
    "key,bad",
    [
        ("account_initial_is_active", None),
        ("account_initial_is_active", "bad"),
        ("account_initial_profile_status", None),
        ("account_initial_profile_status", "x" * 21),
    ],
)
def test_bad_store_and_write_values_refuse_before_mutation(monkeypatch, key, bad):
    monkeypatch.setattr(settings_store, "get_all", lambda db: {key: bad})
    with pytest.raises(ValueError):
        policy.initial_identity_policy(object())
    with pytest.raises(ValueError):
        settings_store.set_many(object(), {key: bad})


def test_null_rows_are_retained_and_factory_requires_actual_store(monkeypatch):
    keys = ("account_initial_is_active", "account_initial_profile_status")
    db = SimpleNamespace(
        execute=lambda statement: SimpleNamespace(all=lambda: [SimpleNamespace(key=key, value=None) for key in keys])
    )
    assert settings_store.get_all(db) == dict.fromkeys(keys)
    with pytest.raises(ValueError, match="settings database session"):
        policy.initial_identity_policy(None)

    def broken(db):
        raise RuntimeError("unavailable")

    monkeypatch.setattr(settings_store, "get_all", broken)
    with pytest.raises(RuntimeError, match="unavailable"):
        policy.initial_identity_policy(object())


def test_factory_uses_strict_session_once(monkeypatch):
    calls = []
    db = object()

    def configured(session, *, require_store):
        calls.append((session, require_store))
        return SimpleNamespace(account_initial_is_active=False, account_initial_profile_status="suspended")

    monkeypatch.setattr(policy, "effective", configured)
    assert policy.initial_identity_policy(db) == policy.InitialIdentityPolicy(False, "suspended")
    assert calls == [(db, True)]
