"""Future owner composition canaries: authored for hosted CI, never run locally."""

from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest
from dotmac_learning import BulkEnrollmentInitialState, InvitationEnrollmentInitialState

from app.config import ENROLLMENT_INITIAL_STATE_POLICY_KEYS, Settings, validate_enrollment_initial_state_policy_value
from app.services import invitation_policy, roster_policy, settings_store


class StringSubclass(str):
    pass


def test_defaults_environment_and_configured_factories(monkeypatch):
    configured = Settings(_env_file=None)
    assert configured.bulk_enrollment_initial_role == "student"
    assert configured.bulk_enrollment_initial_status == "active"
    assert configured.invitation_enrollment_initial_status == "active"
    for key, value in zip(ENROLLMENT_INITIAL_STATE_POLICY_KEYS, ("mentor", "dropped", "waitlisted"), strict=True):
        monkeypatch.setenv(key.upper(), value)
    configured = Settings(_env_file=None)
    monkeypatch.setattr(settings_store, "settings", configured)
    monkeypatch.setattr(settings_store, "get_all", lambda db: {})
    bulk = roster_policy.bulk_enrollment_policy(object())
    invite = invitation_policy.invitation_enrollment_policy(object())
    assert type(bulk) is BulkEnrollmentInitialState
    assert type(invite.initial_state) is InvitationEnrollmentInitialState
    assert (bulk.role_in_cohort, bulk.status) == ("mentor", "dropped")
    assert invite.initial_state.status == "waitlisted"
    with pytest.raises(FrozenInstanceError):
        bulk.status = "active"
    with pytest.raises(FrozenInstanceError):
        invite.initial_state.status = "active"


@pytest.mark.parametrize("key", ENROLLMENT_INITIAL_STATE_POLICY_KEYS)
@pytest.mark.parametrize("bad", [None, True, 1, 1.0, [], {}, StringSubclass("active"), "界" * 21])
def test_invalid_value_refuses_at_settings_store_write_read_and_factory(monkeypatch, key, bad):
    with pytest.raises(ValueError):
        validate_enrollment_initial_state_policy_value(key, bad)
    with pytest.raises(ValueError):
        Settings(_env_file=None, **{key: bad})
    with pytest.raises(ValueError):
        settings_store.set_many(object(), {key: bad})
    monkeypatch.setattr(settings_store, "get_all", lambda db: {key: bad})
    with pytest.raises(ValueError):
        settings_store.effective(object(), require_store=True)
    factory = invitation_policy.invitation_enrollment_policy if key.startswith("invitation") else roster_policy.bulk_enrollment_policy
    with pytest.raises(ValueError):
        factory(object())


@pytest.mark.parametrize("key", ENROLLMENT_INITIAL_STATE_POLICY_KEYS)
@pytest.mark.parametrize("value", ["", "  ", " arbitrary ", "界" * 20])
def test_exact_unicode_character_boundary_and_no_normalization(monkeypatch, key, value):
    assert validate_enrollment_initial_state_policy_value(key, value) == value
    assert getattr(Settings(_env_file=None, **{key: value}), key) == value
    monkeypatch.setenv(key.upper(), value)
    assert getattr(Settings(_env_file=None), key) == value
    monkeypatch.setattr(settings_store, "get_all", lambda db: {key: value})
    assert getattr(settings_store.effective(object(), require_store=True), key) == value
    if key.startswith("invitation"):
        assert invitation_policy.invitation_enrollment_policy(object()).initial_state.status == value
    else:
        bulk = roster_policy.bulk_enrollment_policy(object())
        assert getattr(bulk, "role_in_cohort" if key.endswith("role") else "status") == value


@pytest.mark.parametrize("key", ENROLLMENT_INITIAL_STATE_POLICY_KEYS)
def test_oversize_environment_and_missing_effective_key_refuse(monkeypatch, key):
    monkeypatch.setenv(key.upper(), "界" * 21)
    with pytest.raises(ValueError):
        Settings(_env_file=None)
    configured = dict(bulk_enrollment_initial_role="student", bulk_enrollment_initial_status="active",
                      invitation_enrollment_initial_status="active", invitation_member_role_map={},
                      invitation_default_member_role="student")
    del configured[key]
    module, factory = (invitation_policy, invitation_policy.invitation_enrollment_policy) if key.startswith("invitation") else (roster_policy, roster_policy.bulk_enrollment_policy)
    monkeypatch.setattr(module, "effective", lambda db, *, require_store: SimpleNamespace(**configured))
    with pytest.raises(ValueError, match="missing effective"):
        factory(object())


@pytest.mark.parametrize("factory", [roster_policy.bulk_enrollment_policy, invitation_policy.invitation_enrollment_policy])
def test_required_session_and_store_failure_refuse(monkeypatch, factory):
    with pytest.raises(ValueError, match="settings database session"):
        factory(None)
    failure = RuntimeError("settings read refused")

    def failed(db):
        raise failure

    monkeypatch.setattr(settings_store, "get_all", failed)
    with pytest.raises(RuntimeError) as caught:
        factory(object())
    assert caught.value is failure


def test_factories_each_pin_one_effective_snapshot_and_database_overrides_invalid_unused_environment(monkeypatch):
    for key in ENROLLMENT_INITIAL_STATE_POLICY_KEYS:
        monkeypatch.setattr(settings_store.settings, key, None)
    stored = dict(bulk_enrollment_initial_role="mentor", bulk_enrollment_initial_status="dropped",
                  invitation_enrollment_initial_status="waitlisted", invitation_member_role_map='{"mentor":"instructor"}')
    reads = []

    def get_all(db):
        reads.append(db)
        return dict(stored)

    monkeypatch.setattr(settings_store, "get_all", get_all)
    db = object()
    bulk = roster_policy.bulk_enrollment_policy(db)
    assert reads == [db]
    invite = invitation_policy.invitation_enrollment_policy(db)
    assert reads == [db, db]
    stored.update(bulk_enrollment_initial_role="student", bulk_enrollment_initial_status="active",
                  invitation_enrollment_initial_status="active", invitation_member_role_map='{}')
    assert (bulk.role_in_cohort, bulk.status) == ("mentor", "dropped")
    assert invite.initial_state.status == "waitlisted"
    assert invite.member_role("mentor") == "instructor"


def test_null_rows_are_visible_and_real_settings_roundtrip(admin_session):
    from app.models.platform_settings import PlatformSetting

    configured = dict(bulk_enrollment_initial_role="mentor", bulk_enrollment_initial_status="dropped",
                      invitation_enrollment_initial_status="waitlisted")
    settings_store.set_many(admin_session, configured)
    bulk = roster_policy.bulk_enrollment_policy(admin_session)
    assert (bulk.role_in_cohort, bulk.status) == ("mentor", "dropped")
    assert invitation_policy.invitation_enrollment_policy(admin_session).initial_state.status == "waitlisted"
    for key in ENROLLMENT_INITIAL_STATE_POLICY_KEYS:
        settings_store.delete(admin_session, key)
        admin_session.add(PlatformSetting(key=key, value=None))
        admin_session.flush()
        assert settings_store.get_all(admin_session)[key] is None
        with pytest.raises(ValueError):
            settings_store.effective(admin_session, require_store=True)
        settings_store.delete(admin_session, key)


def test_invitation_policy_requires_distinct_type_and_fresh_validation():
    with pytest.raises(TypeError):
        invitation_policy.AcademyInvitationEnrollmentPolicy({}, "student")
    with pytest.raises(ValueError):
        invitation_policy.AcademyInvitationEnrollmentPolicy({}, "student", BulkEnrollmentInitialState(role_in_cohort="student", status="active"))
    state = InvitationEnrollmentInitialState(status="waitlisted")
    configured = invitation_policy.AcademyInvitationEnrollmentPolicy({}, "student", state)
    assert configured.initial_state is not state
    object.__setattr__(state, "status", None)
    assert configured.initial_state.status == "waitlisted"
    with pytest.raises(ValueError):
        invitation_policy.AcademyInvitationEnrollmentPolicy({}, "student", state)
