"""Entrance invitation policy canaries, authored SOURCE ONLY and NOT RUN.

Actual DB cases use the genuine Settings store and existing entrance owners.
Access-extension success now uses the genuine human boundary; runtime remains unqualified.
The existing Settings store is platform-wide; applicant effects remain scoped.
"""

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from dotmac_kernel.session_runtime import resolve_database_runtime
from sqlalchemy import func, select, text

from app.config import Settings, validate_entrance_invitation_deadline_days
from app.models.admissions import Applicant
from app.models.email_outbox import EmailOutbox
from app.models.platform_settings import PlatformSetting
from app.services import admissions, entrance_exam, settings_store
from app.services import entrance_invitation_policy as policy
from app.services.audit_context import HumanOperationError
from legacy_probe.services.admission_request_fixtures import admission_admin_request
from legacy_probe.services.test_canonical_current_user import current_account
from legacy_probe.services.test_entrance_exam import _applicant, _cohort

assert current_account
KEY = "entrance_invitation_deadline_days"


def _stored(db, value):
    row = db.get(PlatformSetting, KEY)
    if row is None:
        db.add(PlatformSetting(key=KEY, value=value))
    else:
        row.value = value
    db.flush()


def _snapshot(applicant):
    return tuple((column.key, getattr(applicant, column.key)) for column in Applicant.__table__.columns)


def _outbox_count(db):
    return db.scalar(select(func.count()).select_from(EmailOutbox))


def test_default_is_declared_only_in_settings_and_environment_override_is_parsed(monkeypatch):
    monkeypatch.delenv(KEY.upper(), raising=False)
    assert Settings(_env_file=None).entrance_invitation_deadline_days == 7
    monkeypatch.setenv(KEY.upper(), "13")
    configured = Settings(_env_file=None)
    assert configured.entrance_invitation_deadline_days == 13
    monkeypatch.setattr(settings_store, "settings", configured)
    monkeypatch.setattr(settings_store, "get_all", lambda db: {})
    assert policy.entrance_invitation_policy(object()).deadline_days == 13


@pytest.mark.parametrize("bad", [None, True, False, 0, -1, 1.0, "", "0", "-1", "2.5", [], {}])
def test_invalid_configured_values_refuse_in_settings_and_before_store_mutations(bad):
    with pytest.raises(ValueError):
        validate_entrance_invitation_deadline_days(bad)
    with pytest.raises(ValueError):
        Settings(_env_file=None, entrance_invitation_deadline_days=bad)
    # The real writer validates all proposals before attempting db.get/add/flush.
    with pytest.raises(ValueError):
        settings_store.set_many(object(), {KEY: bad})


@pytest.mark.parametrize("bad", [True, False, 0, -1, "3", 1.0, [], {}])
def test_explicit_typed_command_value_is_strict_and_never_coerced(bad):
    with pytest.raises(ValueError):
        policy.entrance_invitation_policy(object(), deadline_days=bad)


def test_immutable_single_snapshot_and_explicit_override_without_second_read(monkeypatch):
    calls = []
    session = object()
    values = SimpleNamespace(entrance_invitation_deadline_days=11)

    def configured(db, *, require_store):
        calls.append((db, require_store))
        return values

    monkeypatch.setattr(policy, "effective", configured)
    selected = policy.entrance_invitation_policy(session)
    values.entrance_invitation_deadline_days = 19
    now = datetime(2026, 10, 19, tzinfo=UTC)
    assert selected.deadline(now) == now + timedelta(days=11)
    assert calls == [(session, True)]
    with pytest.raises(FrozenInstanceError):
        selected.deadline_days = 1
    explicit = policy.entrance_invitation_policy(session, deadline_days=3)
    assert explicit.deadline(now) == now + timedelta(days=3)
    assert calls == [(session, True)]
    with pytest.raises(TypeError):
        policy.EntranceInvitationPolicy()
    object.__setattr__(selected, "deadline_days", True)
    with pytest.raises(ValueError):
        selected.deadline(now)


def test_missing_or_unreadable_store_and_missing_effective_key_refuse(monkeypatch):
    with pytest.raises(ValueError, match="settings database session"):
        policy.entrance_invitation_policy(None)

    def unavailable(db):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(settings_store, "get_all", unavailable)
    with pytest.raises(RuntimeError, match="store unavailable"):
        policy.entrance_invitation_policy(object())
    monkeypatch.setattr(policy, "effective", lambda db, *, require_store: SimpleNamespace())
    with pytest.raises(ValueError, match="missing effective entrance invitation policy"):
        policy.entrance_invitation_policy(object())


def test_actual_settings_store_override_precedence_live_default_and_pinned_snapshot(admin_session, monkeypatch):
    monkeypatch.setattr(settings_store.settings, KEY, 5)
    settings_store.set_many(admin_session, {KEY: "11"})
    first = policy.entrance_invitation_policy(admin_session)
    assert first.deadline_days == 11
    settings_store.set_many(admin_session, {KEY: "13"})
    assert policy.entrance_invitation_policy(admin_session).deadline_days == 13
    assert first.deadline_days == 11
    settings_store.delete(admin_session, KEY)
    assert policy.entrance_invitation_policy(admin_session).deadline_days == 5


@pytest.mark.parametrize("bad", [None, "malformed", "0", "-1", "true", "1.5"])
def test_actual_malformed_stored_policy_is_retained_and_refused(admin_session, bad):
    _stored(admin_session, bad)
    assert settings_store.get_all(admin_session)[KEY] == bad
    with pytest.raises(ValueError):
        settings_store.effective(admin_session, require_store=True)
    with pytest.raises(ValueError):
        policy.entrance_invitation_policy(admin_session)


@pytest.mark.parametrize("operation", ["invite", "reset_and_invite", "extend_access"])
def test_actual_policy_rejection_precedes_any_applicant_token_reset_or_outbox_mutation(
    admin_session, tenant_a, operation
):
    actor_request = admission_admin_request(admin_session, tenant_a, transport="web")
    applicant = _applicant(admin_session, tenant_a, _cohort(admin_session, tenant_a))
    entrance_exam.issue_token(admin_session, applicant=applicant)
    if operation == "extend_access":
        applicant.assessment_started_at = None
    applicant.assessment_answers = {"old": ["A"]}
    applicant.assessment_score = 0.75
    applicant.assessment_deadline = datetime.now(UTC) + timedelta(days=1)
    admin_session.flush()
    _stored(admin_session, "malformed")
    before, count = _snapshot(applicant), _outbox_count(admin_session)
    expected_error = HumanOperationError if operation == "extend_access" else ValueError
    expected_message = "no operation change was saved" if operation == "extend_access" else "positive integer"
    with pytest.raises(expected_error, match=expected_message):
        if operation == "invite":
            entrance_exam.invite(admin_session, applicant=applicant, base_url="https://academy.example")
        elif operation == "reset_and_invite":
            entrance_exam.reset_and_invite(admin_session, applicant=applicant, base_url="https://academy.example")
        else:
            admissions.extend_applicant_access(admin_session, request=actor_request, applicant_id=applicant.id)
    admin_session.refresh(applicant)
    assert _snapshot(applicant) == before
    assert _outbox_count(admin_session) == count


@pytest.mark.parametrize("operation", ["invite", "reset_and_invite"])
@pytest.mark.parametrize("days", [True, "3", 0, -1, 1.5, 10**400])
def test_invalid_or_overflowing_explicit_days_precede_actual_token_and_reset_effects(
    admin_session, tenant_a, operation, days
):
    applicant = _applicant(admin_session, tenant_a, _cohort(admin_session, tenant_a))
    entrance_exam.issue_token(admin_session, applicant=applicant)
    applicant.assessment_answers = {"preserve": ["A"]}
    admin_session.flush()
    before, count = _snapshot(applicant), _outbox_count(admin_session)
    with pytest.raises(ValueError):
        getattr(entrance_exam, operation)(
            admin_session, applicant=applicant, base_url="https://academy.example", deadline_days=days
        )
    admin_session.refresh(applicant)
    assert _snapshot(applicant) == before and _outbox_count(admin_session) == count


@pytest.mark.parametrize("days", [1, 10**400])
def test_only_native_arithmetic_representability_bounds_without_business_clamp(days):
    selected = policy.EntranceInvitationPolicy(days)
    with pytest.raises(ValueError, match="not representable"):
        selected.deadline(datetime.max.replace(tzinfo=UTC))


@pytest.mark.parametrize("operation", ["invite", "reset_and_invite"])
@pytest.mark.parametrize("explicit", [None, 3])
def test_actual_invitation_and_reset_use_configured_nondefault_or_explicit_days(
    admin_session, tenant_a, operation, explicit
):
    settings_store.set_many(admin_session, {KEY: "11"})
    applicant = _applicant(admin_session, tenant_a, _cohort(admin_session, tenant_a))
    before = datetime.now(UTC)
    if operation == "invite":
        result = entrance_exam.invite(
            admin_session, applicant=applicant, base_url="https://academy.example", deadline_days=explicit, send=False
        )
        assert (
            entrance_exam.applicant_for_token(admin_session, tenant_id=tenant_a.id, raw=result["token"]).id
            == applicant.id
        )
    else:
        entrance_exam.reset_and_invite(
            admin_session, applicant=applicant, base_url="https://academy.example", deadline_days=explicit
        )
        assert applicant.assessment_reset_count == 1
    after = datetime.now(UTC)
    days = 11 if explicit is None else explicit
    assert before + timedelta(days=days) <= applicant.assessment_deadline <= after + timedelta(days=days)


def test_access_extension_uses_same_configured_deadline_pending_separate_legacy_audit_port(admin_session, tenant_a):
    actor_request = admission_admin_request(admin_session, tenant_a, transport="web")
    settings_store.set_many(admin_session, {KEY: "11"})
    applicant = _applicant(admin_session, tenant_a, _cohort(admin_session, tenant_a), started=False)
    entrance_exam.issue_token(admin_session, applicant=applicant)
    # Actual genuine human boundary owns the extension and canonical receipt.
    before = datetime.now(UTC)
    result = admissions.extend_applicant_access(admin_session, request=actor_request, applicant_id=applicant.id)
    after = datetime.now(UTC)
    assert result is applicant
    assert before + timedelta(days=11) <= result.assessment_deadline <= after + timedelta(days=11)


def test_shared_actual_policy_preserves_tenant_scope_and_isolates_token_outbox_effects(
    current_account, tenant_b, request
):
    a = current_account
    prior = a.admin.get(PlatformSetting, KEY)
    previous = (prior is not None, prior.value if prior is not None else None)

    def restore_global_setting():
        a.db.rollback()
        row = a.admin.get(PlatformSetting, KEY)
        if previous[0]:
            _stored(a.admin, previous[1])
        elif row is not None:
            a.admin.delete(row)
        a.admin.commit()

    request.addfinalizer(restore_global_setting)
    settings_store.set_many(a.admin, {KEY: "11"})
    first = _applicant(a.admin, a.tenant, _cohort(a.admin, a.tenant))
    other = _applicant(a.admin, tenant_b, _cohort(a.admin, tenant_b))
    first_id, other_id = first.id, other.id
    a.admin.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert a.db.scalar(text("SELECT current_user")) == "app_user"
    root = a.db.get_transaction()
    scoped = a.db.get(Applicant, first_id)
    result = entrance_exam.invite(a.db, applicant=scoped, base_url="https://academy.example")
    assert a.db.get_transaction() is root
    assert a.db.scalar(text("SELECT current_setting('app.current_tenant')")) == str(a.tid)
    assert entrance_exam.applicant_for_token(a.db, tenant_id=tenant_b.id, raw=result["token"]) is None
    assert a.db.get(Applicant, other_id) is None
    assert a.db.scalars(select(EmailOutbox.tenant_id).where(EmailOutbox.kind == "entrance_invite")).all() == [a.tid]
    a.db.commit()
    a.admin.refresh(other)
    assert other.assessment_token_hash is None and other.assessment_deadline is None
    # The same global policy is read under the other caller's explicit tenant scope.
    resolve_database_runtime().set_tenant(a.db, tenant_b.id)
    assert policy.entrance_invitation_policy(a.db).deadline_days == 11
    second = a.db.get(Applicant, other_id)
    entrance_exam.invite(a.db, applicant=second, base_url="https://academy.example")
    assert a.db.scalar(text("SELECT current_setting('app.current_tenant')")) == str(tenant_b.id)
    assert a.db.scalars(select(EmailOutbox.tenant_id).where(EmailOutbox.kind == "entrance_invite")).all() == [
        tenant_b.id
    ]
