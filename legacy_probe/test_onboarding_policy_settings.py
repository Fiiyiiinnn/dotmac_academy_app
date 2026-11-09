"""Strict Settings/env policy tests, authored for hosted CI only."""

import json
from dataclasses import FrozenInstanceError

import pytest

from app.config import Settings, validate_onboarding_policy_value
from app.services import onboarding_policy as policy
from app.services import settings_store
from app.services.invitation_policy import invitation_issuance_policy


def test_defaults_live_only_in_settings_environment(monkeypatch):
    configured = Settings(_env_file=None)
    assert configured.onboarding_enrollment_global_role == "student"
    assert configured.invitation_onboarding_replace_outstanding is True
    catalog = configured.onboarding_task_catalog
    assert [key for key, label in catalog] == ["entrance_assessment", "confirm_details", "orientation"]
    monkeypatch.setenv("ONBOARDING_ENROLLMENT_GLOBAL_ROLE", "custom")
    monkeypatch.setenv("INVITATION_ONBOARDING_REPLACE_OUTSTANDING", "false")
    monkeypatch.setenv("ONBOARDING_TASK_CATALOG", json.dumps(list(reversed(catalog))))
    configured = Settings(_env_file=None)
    assert configured.onboarding_enrollment_global_role == "custom"
    assert configured.invitation_onboarding_replace_outstanding is False
    assert configured.onboarding_task_catalog == tuple(reversed(catalog))


@pytest.mark.parametrize(
    "key,bad",
    [
        ("onboarding_enrollment_global_role", None),
        ("onboarding_enrollment_global_role", ""),
        ("onboarding_enrollment_global_role", "x" * 64),
        ("onboarding_enrollment_global_role", True),
        ("onboarding_task_catalog", None),
        ("onboarding_task_catalog", []),
        ("onboarding_task_catalog", "null"),
        ("onboarding_task_catalog", "malformed"),
        ("onboarding_task_catalog", [["orientation", "x"]] * 3),
        ("onboarding_task_catalog", [["entrance_assessment", "a"], ["confirm_details", "b"], ["unknown", "c"]]),
    ],
)
def test_null_malformed_duplicate_and_unknown_procedures_refuse(monkeypatch, key, bad):
    with pytest.raises(ValueError):
        validate_onboarding_policy_value(key, bad)
    monkeypatch.setattr(settings_store, "get_all", lambda db: {key: bad})
    with pytest.raises(ValueError):
        policy.onboarding_policy(object())
    with pytest.raises(ValueError):
        settings_store.set_many(object(), {key: bad})


def test_db_over_env_order_labels_and_policy_pinning(monkeypatch):
    catalog = tuple(reversed(Settings(_env_file=None).onboarding_task_catalog))
    stored = {
        "onboarding_enrollment_global_role": "custom",
        "onboarding_task_catalog": json.dumps(catalog),
        "invitation_onboarding_replace_outstanding": "false",
    }
    monkeypatch.setattr(settings_store, "get_all", lambda db: stored)
    selected = policy.onboarding_policy(object())
    invitation = invitation_issuance_policy(object(), purpose="onboarding")
    assert selected.global_role == "custom" and selected.task_catalog == catalog
    assert invitation.replace_outstanding is False
    stored.clear()
    assert policy.onboarding_policy(object()).global_role == "student"
    assert selected.global_role == "custom" and invitation.replace_outstanding is False
    with pytest.raises(FrozenInstanceError):
        selected.global_role = "different"
    with pytest.raises(TypeError):
        selected.role_provisioning["new"] = "New"


def test_required_store_errors_refuse(monkeypatch):
    with pytest.raises(ValueError, match="settings database session"):
        policy.onboarding_policy(None)

    def failed(db):
        raise RuntimeError("store failed")

    monkeypatch.setattr(settings_store, "get_all", failed)
    with pytest.raises(RuntimeError, match="store failed"):
        policy.onboarding_policy(object())
    with pytest.raises(RuntimeError, match="store failed"):
        invitation_issuance_policy(object(), purpose="onboarding")


def test_role_slug_storage_boundary_is_exactly_63_in_settings_env_store_and_factory(monkeypatch):
    key = "onboarding_enrollment_global_role"
    valid, invalid = "x" * 63, "x" * 64
    assert validate_onboarding_policy_value(key, valid) == valid
    assert Settings(_env_file=None, **{key: valid}).onboarding_enrollment_global_role == valid
    with pytest.raises(ValueError):
        validate_onboarding_policy_value(key, invalid)
    with pytest.raises(ValueError):
        Settings(_env_file=None, **{key: invalid})
    monkeypatch.setenv("ONBOARDING_ENROLLMENT_GLOBAL_ROLE", valid)
    assert Settings(_env_file=None).onboarding_enrollment_global_role == valid
    monkeypatch.setenv("ONBOARDING_ENROLLMENT_GLOBAL_ROLE", invalid)
    with pytest.raises(ValueError):
        Settings(_env_file=None)
    # Stored override and the actual factory share the same strict boundary.
    monkeypatch.setattr(settings_store, "get_all", lambda db: {key: valid})
    assert settings_store.effective(object(), require_store=True).onboarding_enrollment_global_role == valid
    assert policy.onboarding_policy(object()).global_role == valid
    monkeypatch.setattr(settings_store, "get_all", lambda db: {key: invalid})
    with pytest.raises(ValueError):
        settings_store.effective(object(), require_store=True)
    with pytest.raises(ValueError):
        policy.onboarding_policy(object())
    with pytest.raises(ValueError):
        settings_store.set_many(object(), {key: invalid})
