"""Reset-only policy composition; runtime execution belongs to hosted CI."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from dotmac_kernel.credential_lifecycle import CredentialPolicyViolation, CredentialSubjectRef
from pydantic import ValidationError

from app.config import Settings
from app.services import credential_policy, settings_store
from app.services.credential_policy import AcademyPasswordPolicy, password_reset_policy


def test_default_and_environment_are_configuration(monkeypatch):
    monkeypatch.delenv("PASSWORD_RESET_MIN_LENGTH", raising=False)
    assert Settings(_env_file=None).password_reset_min_length == 8
    monkeypatch.setenv("PASSWORD_RESET_MIN_LENGTH", "13")
    assert Settings(_env_file=None).password_reset_min_length == 13


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.5, "", "true", "8.0", "invalid"])
def test_config_refuses_invalid_minimum(value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, password_reset_min_length=value)


def test_invalid_environment_value_refuses(monkeypatch):
    monkeypatch.setenv("PASSWORD_RESET_MIN_LENGTH", "false")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_malformed_live_configuration_refuses(monkeypatch):
    monkeypatch.setattr(settings_store.settings, "password_reset_min_length", False)
    with pytest.raises(ValueError, match="password_reset_min_length"):
        password_reset_policy(Mock())


def test_settings_store_declares_and_reads_configuration(monkeypatch):
    monkeypatch.setattr(settings_store.settings, "password_reset_min_length", 11)
    assert "password_reset_min_length" in settings_store.KNOWN_KEYS
    assert settings_store.effective().password_reset_min_length == 11


def test_database_value_overrides_environment_and_factory_uses_real_session(monkeypatch):
    monkeypatch.setattr(settings_store.settings, "password_reset_min_length", 12)
    db = Mock()
    reader = Mock(return_value={"password_reset_min_length": "5"})
    monkeypatch.setattr(settings_store, "get_all", reader)
    policy = password_reset_policy(db)
    reader.assert_called_once_with(db)
    assert policy.minimum_length == 5
    policy.validate_account("a" * 5)
    with pytest.raises(CredentialPolicyViolation):
        policy.validate_account("a" * 4)


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.5, "", "bad", "8.0"])
def test_malformed_database_policy_refuses_instead_of_fallback(monkeypatch, value):
    monkeypatch.setattr(settings_store, "get_all", lambda db: {"password_reset_min_length": value})
    with pytest.raises(ValueError, match="password_reset_min_length"):
        password_reset_policy(Mock())


def test_failed_database_read_cannot_authorize_reset(monkeypatch):
    reader = Mock(side_effect=RuntimeError("store unavailable"))
    monkeypatch.setattr(settings_store, "get_all", reader)
    with pytest.raises(RuntimeError, match="store unavailable"):
        password_reset_policy(Mock())
    with pytest.raises(ValueError, match="session"):
        password_reset_policy(None)


def test_existing_nonstrict_callers_keep_best_effort_reads(monkeypatch):
    monkeypatch.setattr(settings_store.settings, "password_reset_min_length", 10)
    monkeypatch.setattr(settings_store, "get_all", Mock(side_effect=RuntimeError("store unavailable")))
    assert settings_store.effective(Mock()).password_reset_min_length == 10


def test_absent_database_override_uses_configuration(monkeypatch):
    monkeypatch.setattr(settings_store.settings, "password_reset_min_length", 9)
    monkeypatch.setattr(settings_store, "get_all", lambda db: {})
    assert password_reset_policy(Mock()).minimum_length == 9


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.5, "", "bad", "8.0"])
def test_invalid_policy_write_refused_before_any_mutation(value):
    db = Mock()
    with pytest.raises(ValueError, match="password_reset_min_length"):
        settings_store.set_many(db, {"password_reset_min_length": value})
    db.get.assert_not_called()
    db.add.assert_not_called()
    db.flush.assert_not_called()


def test_valid_policy_write_flushes_without_committing():
    db = Mock()
    existing = SimpleNamespace(value="8")
    db.get.return_value = existing
    settings_store.set_many(db, {"password_reset_min_length": "14"})
    assert existing.value == "14"
    db.flush.assert_called_once_with()
    db.commit.assert_not_called()


@pytest.mark.parametrize("value", [True, False, 0, -1, "8", None, 2.0])
def test_policy_requires_typed_positive_integer(value):
    with pytest.raises(ValueError, match="minimum_length"):
        AcademyPasswordPolicy(value)


def test_factory_refuses_unknown_or_malformed_effective_shape(monkeypatch):
    for resolved in (SimpleNamespace(), SimpleNamespace(password_reset_min_length=True)):
        monkeypatch.setattr(credential_policy, "effective", lambda db, resolved=resolved, **kwargs: resolved)
        with pytest.raises((AttributeError, ValueError)):
            password_reset_policy(Mock())


def test_policy_has_early_and_real_subject_port_methods():
    policy = AcademyPasswordPolicy(3)
    subject = CredentialSubjectRef("academy", "person", "actual-person", "actual-credential")
    policy.validate_account("abc")
    policy.validate(subject, "abc")
    for validate in (policy.validate_account, lambda secret: policy.validate(subject, secret)):
        with pytest.raises(CredentialPolicyViolation) as caught:
            validate("xy")
        assert "xy" not in str(caught.value)
    with pytest.raises(CredentialPolicyViolation):
        policy.validate(object(), "abc")
