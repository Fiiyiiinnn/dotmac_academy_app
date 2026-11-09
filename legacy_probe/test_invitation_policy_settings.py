"""Invitation configuration and fail-closed composition canaries; hosted CI only."""

from types import SimpleNamespace

import pytest
from dotmac_learning import InvitationEnrollmentInitialState

from app.config import Settings, validate_invitation_default_member_role, validate_invitation_member_role_map
from app.services import invitation_policy as policy
from app.services import settings_store


def test_settings_defaults_preserve_the_existing_product_role_policy():
    configured = Settings(_env_file=None)
    assert configured.invitation_member_role_map == {"admin": "instructor", "instructor": "instructor"}
    assert configured.invitation_default_member_role == "student"


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        True,
        1,
        "{",
        "[]",
        '{"a":"student","a":"instructor"}',
        {"a": "admin"},
        {"": "student"},
        {1: "student"},
        {"a": None},
    ],
)
def test_invalid_mapping_is_refused_without_echoing_material(bad):
    with pytest.raises(ValueError) as caught:
        validate_invitation_member_role_map(bad)
    assert str(caught.value) == "invitation_member_role_map must map nonempty role names to student or instructor"


@pytest.mark.parametrize("bad", [None, True, 1, "admin", " student", "STUDENT", {}])
def test_invalid_default_is_refused(bad):
    with pytest.raises(ValueError, match="invitation_default_member_role must be student or instructor"):
        validate_invitation_default_member_role(bad)


def test_policy_requires_both_inputs_and_freezes_a_validated_snapshot():
    mapping = {"mentor": "instructor"}
    configured = policy.AcademyInvitationEnrollmentPolicy(mapping, "student", InvitationEnrollmentInitialState(status="active"))
    mapping["mentor"] = "student"
    assert configured.member_role("mentor") == "instructor"
    assert configured.member_role("admin") == "student"
    assert configured.member_role("instructor") == "student"
    assert configured.member_role("any-other-role") == "student"
    with pytest.raises(TypeError):
        configured.role_map["mentor"] = "student"
    with pytest.raises(TypeError):
        policy.AcademyInvitationEnrollmentPolicy({})
    alternate = policy.AcademyInvitationEnrollmentPolicy({}, "instructor", InvitationEnrollmentInitialState(status="active"))
    assert alternate.member_role("student") == "instructor"


def test_factory_uses_strict_store_and_actual_session(monkeypatch):
    db = object()
    calls = []

    def effective(session, *, require_store):
        calls.append((session, require_store))
        return SimpleNamespace(
            invitation_member_role_map={"mentor": "instructor"}, invitation_default_member_role="student",
            invitation_enrollment_initial_status="active",
        )

    monkeypatch.setattr(policy, "effective", effective)
    configured = policy.invitation_enrollment_policy(db)
    assert calls == [(db, True)]
    assert configured.member_role("mentor") == "instructor"


def test_database_overrides_environment_and_configured_fallback(monkeypatch):
    monkeypatch.setattr(settings_store.settings, "invitation_member_role_map", {"env-only": "instructor"})
    monkeypatch.setattr(settings_store.settings, "invitation_default_member_role", "student")
    monkeypatch.setattr(
        settings_store,
        "get_all",
        lambda db: {
            "invitation_member_role_map": '{"db-only":"student"}',
            "invitation_default_member_role": "instructor",
        },
    )
    configured = policy.invitation_enrollment_policy(object())
    assert configured.member_role("db-only") == "student"
    assert configured.member_role("env-only") == "instructor"
    assert configured.member_role("unmapped") == "instructor"


def test_empty_database_uses_declared_settings(monkeypatch):
    monkeypatch.setattr(settings_store.settings, "invitation_member_role_map", {"configured": "student"})
    monkeypatch.setattr(settings_store.settings, "invitation_default_member_role", "instructor")
    monkeypatch.setattr(settings_store, "get_all", lambda db: {})
    configured = policy.invitation_enrollment_policy(object())
    assert configured.member_role("configured") == "student"
    assert configured.member_role("unknown") == "instructor"


@pytest.mark.parametrize(
    "key,bad",
    [
        ("invitation_member_role_map", None),
        ("invitation_member_role_map", "null"),
        ("invitation_member_role_map", '{"a":"admin"}'),
        ("invitation_default_member_role", None),
        ("invitation_default_member_role", "admin"),
    ],
)
def test_invalid_database_value_never_falls_back(monkeypatch, key, bad):
    monkeypatch.setattr(settings_store, "get_all", lambda db: {key: bad})
    with pytest.raises(ValueError):
        policy.invitation_enrollment_policy(object())


def test_null_database_policy_rows_are_not_hidden():
    rows = [SimpleNamespace(key="invitation_member_role_map", value=None), SimpleNamespace(key="smtp_host", value=None)]
    db = SimpleNamespace(execute=lambda statement: SimpleNamespace(all=lambda: rows))
    assert settings_store.get_all(db) == {"invitation_member_role_map": None}


def test_missing_session_and_store_failure_refuse(monkeypatch):
    with pytest.raises(ValueError, match="settings database session"):
        policy.invitation_enrollment_policy(None)
    failure = RuntimeError("store unavailable")

    def broken(db):
        raise failure

    monkeypatch.setattr(settings_store, "get_all", broken)
    with pytest.raises(RuntimeError) as caught:
        policy.invitation_enrollment_policy(object())
    assert caught.value is failure


@pytest.mark.parametrize(
    "key,bad", [("invitation_member_role_map", '{"mentor":"admin"}'), ("invitation_default_member_role", "admin")]
)
def test_invalid_settings_write_is_refused_before_any_write(key, bad):
    with pytest.raises(ValueError):
        settings_store.set_many(object(), {key: bad})


def test_environment_mapping_and_fallback_are_typed(monkeypatch):
    monkeypatch.setenv("INVITATION_MEMBER_ROLE_MAP", '{"mentor":"student"}')
    monkeypatch.setenv("INVITATION_DEFAULT_MEMBER_ROLE", "instructor")
    configured = Settings(_env_file=None)
    assert configured.invitation_member_role_map == {"mentor": "student"}
    assert configured.invitation_default_member_role == "instructor"


@pytest.mark.parametrize("bad", ['{"mentor":"private-invalid-material"}', "[]", "null"])
def test_settings_validation_does_not_echo_invalid_role_material(bad):
    with pytest.raises(ValueError) as caught:
        Settings(_env_file=None, invitation_member_role_map=bad)
    assert "private-invalid-material" not in str(caught.value)


def test_invalid_environment_json_refuses(monkeypatch):
    monkeypatch.setenv("INVITATION_MEMBER_ROLE_MAP", "{")
    with pytest.raises(ValueError):
        Settings(_env_file=None)
