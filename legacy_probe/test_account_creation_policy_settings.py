"""Settings-backed web account policy canaries; hosted CI execution only."""

from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest
from dotmac_kernel.credential_lifecycle import CredentialPolicyViolation, CredentialSubjectRef
from dotmac_learning import InvitationEnrollmentInitialState

from app.config import (
    Settings,
    validate_web_account_creation_password_min_length,
    validate_web_account_creation_role_admission,
    validate_web_account_creation_role_provisioning,
)
from app.services import account_creation_policy as policy
from app.services import settings_store
from app.services.credential_policy import AcademyPasswordPolicy
from app.services.invitation_policy import AcademyInvitationEnrollmentPolicy


def _subject():
    return CredentialSubjectRef(
        application="academy", principal_kind="user", principal_ref="person", credential_ref="credential"
    )


def test_settings_defaults_preserve_web_creation_without_inheriting_reset_strength():
    configured = Settings(_env_file=None)
    assert configured.web_account_creation_role_admission == {
        "student": ("instructor", "admin"),
        "instructor": ("admin",),
        "admin": ("admin",),
    }
    assert configured.web_account_creation_password_min_length == 0
    assert configured.password_reset_min_length == 8
    assert configured.invitation_default_member_role == "student"


@pytest.mark.parametrize(
    "actors,target,admitted",
    [
        (("instructor",), "student", True),
        (("admin",), "student", True),
        (("admin",), "instructor", True),
        (("admin",), "admin", True),
        (("instructor",), "instructor", False),
        (("instructor",), "admin", False),
        (("student",), "student", False),
        ((), "unknown", False),
        (("admin",), "unknown", False),
    ],
)
def test_default_role_admission_matrix(actors, target, admitted):
    configured = Settings(_env_file=None)
    value = policy.AcademyWebAccountCreationPolicy(
        configured.web_account_creation_role_admission,
        configured.web_account_creation_password_min_length,
        role_provisioning={},
    )
    assert value.can_assign_role(target, actors) is admitted
    assert value.can_create_any(actors) is bool(set(actors) & {"instructor", "admin"})


def test_caller_can_preserve_neither_actor_before_unknown_target_order():
    value = policy.AcademyWebAccountCreationPolicy({"learner": ("mentor",)}, 0, role_provisioning={})

    def status(target, actors):
        if not value.can_create_any(actors):
            return 403
        if target not in value.role_admission:
            return 400
        return 200 if value.can_assign_role(target, actors) else 403

    assert status("unknown", ()) == 403
    assert status("unknown", ("mentor",)) == 400
    assert status("learner", ("mentor",)) == 200


def test_policy_uses_only_configured_custom_roles_and_immutable_snapshot():
    actors = ["coach"]
    mapping = {"trainee": actors}
    value = policy.AcademyWebAccountCreationPolicy(mapping, 0, role_provisioning={})
    actors.append("admin")
    mapping["trainee"] = ["admin"]
    assert value.role_admission == {"trainee": ("coach",)}
    assert value.can_create_any(("coach",))
    assert value.can_assign_role("trainee", ("coach",))
    assert not value.can_create_any(("admin", "instructor"))
    assert not value.can_assign_role("student", ("coach",))
    with pytest.raises(TypeError):
        value.role_admission["trainee"] = ("admin",)
    with pytest.raises(FrozenInstanceError):
        value.minimum_length = 9
    with pytest.raises(TypeError):
        policy.AcademyWebAccountCreationPolicy({}, role_provisioning={})
    denied = policy.AcademyWebAccountCreationPolicy({}, 0, role_provisioning={})
    assert not denied.can_create_any(("admin",))


def test_zero_minimum_is_no_additional_strength_rule_and_keeps_input_types():
    value = policy.AcademyWebAccountCreationPolicy({}, 0, role_provisioning={})
    value.validate_account("")
    value.validate(_subject(), "x")
    with pytest.raises(CredentialPolicyViolation):
        value.validate_account(None)
    with pytest.raises(CredentialPolicyViolation):
        value.validate(object(), "password")


def test_configured_strength_applies_early_and_per_subject_without_material_retention():
    value = policy.AcademyWebAccountCreationPolicy({}, 5, role_provisioning={})
    value.validate_account("12345")
    value.validate(_subject(), "12345")
    for validate in (value.validate_account, lambda secret: value.validate(_subject(), secret)):
        with pytest.raises(CredentialPolicyViolation) as caught:
            validate("weak")
        assert str(caught.value) == "Password does not meet the configured web account creation policy"
        assert "weak" not in str(caught.value)
    assert "12345" not in repr(value)
    assert "weak" not in repr(value)


@pytest.mark.parametrize(
    "bad",
    [
        None,
        True,
        1,
        [],
        "{",
        "[]",
        "null",
        '{"x":["a"],"x":["b"]}',
        {"": ["a"]},
        {" x": ["a"]},
        {1: ["a"]},
        {"x": []},
        {"x": "a"},
        {"x": {"a"}},
        {"x": [True]},
        {"x": [""]},
        {"x": ["a", "a"]},
        {"x": None},
    ],
)
def test_invalid_admission_shapes_refuse_without_value_echo(bad):
    with pytest.raises(ValueError) as caught:
        validate_web_account_creation_role_admission(bad)
    assert (
        str(caught.value)
        == "web_account_creation_role_admission must map role names to nonempty unique actor-role lists"
    )


@pytest.mark.parametrize("bad", [None, True, False, -1, 0.0, 2.5, "-1", "1.0", "true", "", [], {}])
def test_invalid_minimum_refuses_without_coercive_fallback(bad):
    with pytest.raises(ValueError, match="web_account_creation_password_min_length must be a nonnegative integer"):
        validate_web_account_creation_password_min_length(bad)


@pytest.mark.parametrize("raw,expected", [(0, 0), (5, 5), ("0", 0), ("12", 12)])
def test_valid_minimum_accepts_explicit_settings_and_stored_integer_text(raw, expected):
    assert validate_web_account_creation_password_min_length(raw) == expected


def test_factory_uses_actual_session_and_strict_effective(monkeypatch):
    db = object()
    calls = []

    def effective(session, *, require_store):
        calls.append((session, require_store))
        return SimpleNamespace(
            web_account_creation_role_admission={"trainee": ("coach",)},
            web_account_creation_password_min_length=0,
            web_account_creation_role_provisioning={"trainee": "Trainee"},
        )

    monkeypatch.setattr(policy, "effective", effective)
    value = policy.web_account_creation_policy(db)
    assert calls == [(db, True)]
    assert value.can_assign_role("trainee", ("coach",))
    assert value.minimum_length == 0


def test_database_over_environment_custom_roles_and_zero_minimum(monkeypatch):
    monkeypatch.setattr(settings_store.settings, "web_account_creation_role_admission", {"env-target": ("env-actor",)})
    monkeypatch.setattr(settings_store.settings, "web_account_creation_password_min_length", 9)
    monkeypatch.setattr(
        settings_store,
        "get_all",
        lambda db: {
            "web_account_creation_role_admission": '{"db-target":["db-actor"]}',
            "web_account_creation_password_min_length": "0",
        },
    )
    value = policy.web_account_creation_policy(object())
    assert value.can_assign_role("db-target", ("db-actor",))
    assert not value.can_create_any(("env-actor", "admin"))
    assert value.minimum_length == 0
    value.validate_account("x")


def test_empty_database_uses_current_settings_and_preserves_other_effective_keys(monkeypatch):
    before = settings_store._defaults()
    monkeypatch.setattr(settings_store, "get_all", lambda db: {})
    configured = settings_store.effective(object(), require_store=True)
    assert configured.__dict__ == before
    value = policy.web_account_creation_policy(object())
    assert value.role_admission == before["web_account_creation_role_admission"]
    assert value.minimum_length == before["web_account_creation_password_min_length"]


@pytest.mark.parametrize(
    "key,bad",
    [
        ("web_account_creation_role_admission", None),
        ("web_account_creation_role_admission", '{"x":[]}'),
        ("web_account_creation_password_min_length", None),
        ("web_account_creation_password_min_length", "-1"),
    ],
)
def test_bad_database_values_and_null_policy_never_fall_back(monkeypatch, key, bad):
    monkeypatch.setattr(settings_store, "get_all", lambda db: {key: bad})
    with pytest.raises(ValueError):
        policy.web_account_creation_policy(object())


def test_get_all_retains_new_and_prior_invitation_nulls_only():
    names = [
        "web_account_creation_role_admission",
        "web_account_creation_password_min_length",
        "invitation_member_role_map",
        "invitation_default_member_role",
        "password_reset_min_length",
        "smtp_host",
    ]
    rows = [SimpleNamespace(key=name, value=None) for name in names]
    db = SimpleNamespace(execute=lambda statement: SimpleNamespace(all=lambda: rows))
    assert settings_store.get_all(db) == dict.fromkeys(names[:4])


def test_factory_missing_session_and_store_failure_refuse(monkeypatch):
    with pytest.raises(ValueError, match="settings database session"):
        policy.web_account_creation_policy(None)
    failure = RuntimeError("store unavailable")

    def broken(db):
        raise failure

    monkeypatch.setattr(settings_store, "get_all", broken)
    with pytest.raises(RuntimeError) as caught:
        policy.web_account_creation_policy(object())
    assert caught.value is failure


@pytest.mark.parametrize(
    "key,bad",
    [
        ("web_account_creation_role_admission", '{"x":[]}'),
        ("web_account_creation_password_min_length", "-1"),
        ("web_account_creation_role_admission", None),
        ("web_account_creation_password_min_length", None),
    ],
)
def test_invalid_settings_write_refuses_before_database_mutation(key, bad):
    with pytest.raises(ValueError):
        settings_store.set_many(object(), {key: bad})


def test_reset_and_invitation_configuration_remain_independent(monkeypatch):
    monkeypatch.setattr(
        settings_store,
        "get_all",
        lambda db: {
            "web_account_creation_password_min_length": "0",
            "password_reset_min_length": "8",
            "invitation_member_role_map": '{"mentor":"instructor"}',
            "smtp_port": "2525",
        },
    )
    configured = settings_store.effective(object(), require_store=True)
    web = policy.web_account_creation_policy(object())
    reset = AcademyPasswordPolicy(configured.password_reset_min_length)
    invitation = AcademyInvitationEnrollmentPolicy(
        configured.invitation_member_role_map, configured.invitation_default_member_role,
        InvitationEnrollmentInitialState(status=configured.invitation_enrollment_initial_status),
    )
    web.validate_account("x")
    with pytest.raises(CredentialPolicyViolation):
        reset.validate_account("x")
    assert invitation.member_role("mentor") == "instructor"
    assert invitation.member_role("unknown") == "student"
    assert configured.smtp_port == 2525


def test_environment_configuration_and_material_free_validation(monkeypatch):
    monkeypatch.setenv("WEB_ACCOUNT_CREATION_ROLE_ADMISSION", '{"trainee":["coach"]}')
    monkeypatch.setenv("WEB_ACCOUNT_CREATION_PASSWORD_MIN_LENGTH", "4")
    configured = Settings(_env_file=None)
    assert configured.web_account_creation_role_admission == {"trainee": ("coach",)}
    assert configured.web_account_creation_password_min_length == 4
    with pytest.raises(ValueError) as caught:
        Settings(_env_file=None, web_account_creation_role_admission={"private-config-value": []})
    assert "private-config-value" not in str(caught.value)


@pytest.mark.parametrize("bad", [True, False, -1, 0.0, "0", None])
def test_runtime_policy_requires_typed_configured_minimum(bad):
    with pytest.raises(ValueError, match="web_account_creation_password_min_length must be a nonnegative integer"):
        policy.AcademyWebAccountCreationPolicy({}, bad, role_provisioning={})


@pytest.mark.parametrize("bad", ["admin", None, [True], [""]])
def test_actor_collection_shape_never_promotes_untyped_caller_data(bad):
    value = policy.AcademyWebAccountCreationPolicy({"target": ("admin",)}, 0, role_provisioning={})
    for method in (value.can_create_any, lambda actors: value.can_assign_role("target", actors)):
        with pytest.raises(ValueError, match="actor role slugs must be a collection of nonempty role names"):
            method(bad)


@pytest.mark.parametrize(
    "key,bad", [("WEB_ACCOUNT_CREATION_ROLE_ADMISSION", "{"), ("WEB_ACCOUNT_CREATION_PASSWORD_MIN_LENGTH", "true")]
)
def test_malformed_environment_configuration_refuses(monkeypatch, key, bad):
    monkeypatch.setenv(key, bad)
    with pytest.raises(ValueError):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    "key,bad",
    [("web_account_creation_role_admission", {"target": []}), ("web_account_creation_password_min_length", True)],
)
def test_invalid_live_config_refuses_before_store_read(monkeypatch, key, bad):
    monkeypatch.setattr(settings_store.settings, key, bad)
    monkeypatch.setattr(settings_store, "get_all", lambda db: pytest.fail("store read before invalid config refused"))
    with pytest.raises(ValueError):
        policy.web_account_creation_policy(object())


def test_provisioning_defaults_include_all_legacy_role_names_independent_of_selected_target():
    configured = Settings(_env_file=None)
    expected = {"student": "Student", "instructor": "Instructor", "admin": "Administrator"}
    assert configured.web_account_creation_role_provisioning == expected
    value = policy.AcademyWebAccountCreationPolicy(
        configured.web_account_creation_role_admission,
        configured.web_account_creation_password_min_length,
        configured.web_account_creation_role_provisioning,
    )
    assert dict(value.role_provisioning) == expected
    assert tuple(value.role_provisioning) == ("student", "instructor", "admin")


def test_provisioning_is_required_immutable_and_independent_of_admission():
    configured = {"custom": "Custom display", "other": "Another name"}
    value = policy.AcademyWebAccountCreationPolicy({"already-existing": ("actor",)}, 0, configured)
    configured["custom"] = "Changed"
    assert value.role_provisioning == {"custom": "Custom display", "other": "Another name"}
    assert value.can_assign_role("already-existing", ("actor",))
    assert not value.can_assign_role("custom", ("actor",))
    with pytest.raises(TypeError):
        value.role_provisioning["custom"] = "Changed"
    with pytest.raises(FrozenInstanceError):
        value.role_provisioning = {}
    with pytest.raises(TypeError):
        policy.AcademyWebAccountCreationPolicy({}, 0)
    empty = policy.AcademyWebAccountCreationPolicy({"catalog-only": ("actor",)}, 0, {})
    assert empty.role_provisioning == {}
    assert empty.can_assign_role("catalog-only", ("actor",))


@pytest.mark.parametrize(
    "bad",
    [
        None,
        True,
        1,
        [],
        "{",
        "[]",
        "null",
        '{"x":"One","x":"Two"}',
        {"": "Name"},
        {" x": "Name"},
        {1: "Name"},
        {"x": ""},
        {"x": " Name "},
        {"x": None},
        {"x": True},
        {"x": ["Name"]},
    ],
)
def test_invalid_provisioning_mapping_refuses_without_material_echo(bad):
    with pytest.raises(ValueError) as caught:
        validate_web_account_creation_role_provisioning(bad)
    assert str(caught.value) == "web_account_creation_role_provisioning must map role names to nonempty display names"


def test_provisioning_database_override_and_empty_map_are_actual_factory_inputs(monkeypatch):
    monkeypatch.setattr(
        settings_store.settings, "web_account_creation_role_provisioning", {"env-only": "Environment name"}
    )
    monkeypatch.setattr(
        settings_store,
        "get_all",
        lambda db: {
            "web_account_creation_role_provisioning": '{"custom":"Configured name","unrelated":"Unrelated name"}'
        },
    )
    value = policy.web_account_creation_policy(object())
    assert value.role_provisioning == {"custom": "Configured name", "unrelated": "Unrelated name"}
    assert "env-only" not in value.role_provisioning
    monkeypatch.setattr(settings_store, "get_all", lambda db: {"web_account_creation_role_provisioning": "{}"})
    empty = policy.web_account_creation_policy(object())
    assert empty.role_provisioning == {}


@pytest.mark.parametrize("bad", [None, "null", '{"x":""}', '{"x":"One","x":"Two"}'])
def test_bad_or_null_stored_provisioning_refuses_without_fallback(monkeypatch, bad):
    monkeypatch.setattr(settings_store, "get_all", lambda db: {"web_account_creation_role_provisioning": bad})
    with pytest.raises(ValueError):
        policy.web_account_creation_policy(object())


def test_null_provisioning_is_retained_and_invalid_write_refuses_before_mutation():
    db = SimpleNamespace(
        execute=lambda statement: SimpleNamespace(
            all=lambda: [SimpleNamespace(key="web_account_creation_role_provisioning", value=None)]
        )
    )
    assert settings_store.get_all(db) == {"web_account_creation_role_provisioning": None}
    with pytest.raises(ValueError):
        settings_store.set_many(object(), {"web_account_creation_role_provisioning": None})


def test_provisioning_environment_and_validation_errors_are_material_free(monkeypatch):
    monkeypatch.setenv("WEB_ACCOUNT_CREATION_ROLE_PROVISIONING", '{"custom":"Friendly name"}')
    configured = Settings(_env_file=None)
    assert configured.web_account_creation_role_provisioning == {"custom": "Friendly name"}
    with pytest.raises(ValueError) as caught:
        Settings(_env_file=None, web_account_creation_role_provisioning={"private-material": []})
    assert "private-material" not in str(caught.value)


def test_provisioning_store_failure_has_no_hidden_fallback(monkeypatch):
    failure = RuntimeError("settings unavailable")

    def broken(db):
        raise failure

    monkeypatch.setattr(settings_store, "get_all", broken)
    with pytest.raises(RuntimeError) as caught:
        policy.web_account_creation_policy(object())
    assert caught.value is failure
