"""Admission state Settings/factory canaries; authored for hosted CI only."""

from dataclasses import FrozenInstanceError

import pytest
from dotmac_learning import AdmissionEnrollmentInitialState

from app.config import ADMISSION_ENROLLMENT_POLICY_KEYS, Settings, validate_admission_enrollment_policy_value
from app.services import admission_enrollment_policy as policy
from app.services import settings_store


def test_defaults_environment_and_missing_row_fallback(monkeypatch):
    configured = Settings(_env_file=None)
    assert configured.admission_enrollment_initial_role == "student"
    assert configured.admission_enrollment_initial_status == "active"
    monkeypatch.setenv("ADMISSION_ENROLLMENT_INITIAL_ROLE", "instructor")
    monkeypatch.setenv("ADMISSION_ENROLLMENT_INITIAL_STATUS", "dropped")
    configured = Settings(_env_file=None)
    monkeypatch.setattr(settings_store, "settings", configured)
    monkeypatch.setattr(settings_store, "get_all", lambda db: {})
    state = policy.admission_enrollment_policy(object())
    assert type(state) is AdmissionEnrollmentInitialState
    assert (state.role_in_cohort, state.status) == ("instructor", "dropped")


@pytest.mark.parametrize("key", ADMISSION_ENROLLMENT_POLICY_KEYS)
@pytest.mark.parametrize("bad", [None, True, False, 1, 1.0, [], {}, "界" * 21])
def test_strict_settings_store_write_read_and_factory_refusal(monkeypatch, key, bad):
    with pytest.raises(ValueError):
        validate_admission_enrollment_policy_value(key, bad)
    with pytest.raises(ValueError):
        Settings(_env_file=None, **{key: bad})
    with pytest.raises(ValueError):
        settings_store.set_many(object(), {key: bad})
    monkeypatch.setattr(settings_store, "get_all", lambda db: {key: bad})
    with pytest.raises(ValueError):
        settings_store.effective(object(), require_store=True)
    with pytest.raises(ValueError):
        policy.admission_enrollment_policy(object())


@pytest.mark.parametrize("value", ["", "  ", " arbitrary ", "界" * 20])
@pytest.mark.parametrize("key", ADMISSION_ENROLLMENT_POLICY_KEYS)
def test_storage_boundary_accepts_exact_values_without_enum_or_normalization(monkeypatch, key, value):
    assert validate_admission_enrollment_policy_value(key, value) == value
    assert getattr(Settings(_env_file=None, **{key: value}), key) == value
    monkeypatch.setenv(key.upper(), value)
    assert getattr(Settings(_env_file=None), key) == value
    monkeypatch.setattr(settings_store, "get_all", lambda db: {key: value})
    assert getattr(settings_store.effective(object(), require_store=True), key) == value
    state = policy.admission_enrollment_policy(object())
    assert getattr(state, "role_in_cohort" if key.endswith("role") else "status") == value


@pytest.mark.parametrize("key", ADMISSION_ENROLLMENT_POLICY_KEYS)
def test_env_oversize_refuses(monkeypatch, key):
    monkeypatch.setenv(key.upper(), "界" * 21)
    with pytest.raises(ValueError):
        Settings(_env_file=None)


def test_db_override_is_pinned_and_not_masked_by_invalid_unused_env_default(monkeypatch):
    monkeypatch.setattr(settings_store.settings, "admission_enrollment_initial_role", None)
    stored = {"admission_enrollment_initial_role": "instructor", "admission_enrollment_initial_status": "dropped"}
    monkeypatch.setattr(settings_store, "get_all", lambda db: stored)
    selected = policy.admission_enrollment_policy(object())
    stored.update(admission_enrollment_initial_role="student", admission_enrollment_initial_status="active")
    assert (selected.role_in_cohort, selected.status) == ("instructor", "dropped")
    assert policy.admission_enrollment_policy(object()).status == "active"
    with pytest.raises(FrozenInstanceError):
        selected.status = "active"


def test_missing_effective_key_and_required_session_and_read_error_refuse(monkeypatch):
    with pytest.raises(ValueError, match="settings database session"):
        policy.admission_enrollment_policy(None)

    def failed(db):
        raise RuntimeError("settings read refused")

    monkeypatch.setattr(settings_store, "get_all", failed)
    with pytest.raises(RuntimeError, match="settings read refused"):
        policy.admission_enrollment_policy(object())
    monkeypatch.setattr(policy, "effective", lambda db, *, require_store: settings_store.EffectiveSettings({}))
    with pytest.raises(ValueError, match="missing effective admission policy"):
        policy.admission_enrollment_policy(object())


def test_actual_store_roundtrip_override_and_null_row_refusal(admin_session):
    from app.models.platform_settings import PlatformSetting

    settings_store.set_many(
        admin_session,
        {"admission_enrollment_initial_role": "instructor", "admission_enrollment_initial_status": "dropped"},
    )
    state = policy.admission_enrollment_policy(admin_session)
    assert (state.role_in_cohort, state.status) == ("instructor", "dropped")
    settings_store.delete(admin_session, "admission_enrollment_initial_status")
    assert policy.admission_enrollment_policy(admin_session).status == "active"
    admin_session.add(PlatformSetting(key="admission_enrollment_initial_status", value=None))
    admin_session.flush()
    assert settings_store.get_all(admin_session)["admission_enrollment_initial_status"] is None
    with pytest.raises(ValueError):
        policy.admission_enrollment_policy(admin_session)
