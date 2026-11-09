"""Maintained port of legacy_probe/test_entrance_invitation_policy_settings.py.

Issue #2 wave 4: DISPOSITIONS row -> test_entrance_invitation_deadline_policy_strict.
PlatformSetting fixtures port to kernel DomainSetting via settings_store;
kernel storage forbids NULL-valued rows outright, so the stored-NULL case
asserts the insert is refused. Legacy ``admin_session``/``tenant_a``/
``current_account`` fixtures and the ``_applicant``/``_cohort``/
``admission_admin_request`` helpers are ported below onto the maintained
``genesis_database`` fixture.
"""

import uuid
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from dotmac_kernel.models import Party, PartyPerson, PartyRoleGrant, PartyType, Role, Tenant, UserCredential
from dotmac_kernel.session_runtime import resolve_database_runtime
from dotmac_kernel.settings_models import DomainSetting
from sqlalchemy import create_engine, delete, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from app.config import Settings, validate_entrance_invitation_deadline_days
from app.models.admissions import Applicant
from app.models.cohort import Cohort
from app.models.email_outbox import EmailOutbox
from app.models.identity_extensions import AcademyAccountProfile, AcademyCredentialPolicy
from app.services import account_invitations as decisions
from app.services import admissions, entrance_exam, settings_store
from app.services import entrance_invitation_policy as policy
from app.services.audit_context import HumanOperationError
from app.services.credential_authentication import sign_in
from app.services.security import hash_password

KEY = "entrance_invitation_deadline_days"


@pytest.fixture
def admin_session(genesis_database):
    admin, _superuser, _name = genesis_database
    db = sessionmaker(bind=admin, autoflush=False)()
    try:
        yield db
        db.rollback()
    finally:
        db.close()


@pytest.fixture
def app_user_session(genesis_database):
    _admin, superuser, name = genesis_database
    url = make_url(superuser).set(database=name, username="app_user", password=None)
    engine = create_engine(url.render_as_string(hide_password=False), future=True)
    db = sessionmaker(bind=engine, autocommit=False, autoflush=False)()
    try:
        yield db
        db.rollback()
    finally:
        db.close()
        engine.dispose()


def _make_tenant(admin_session, slug, name):
    admin_session.rollback()
    admin_session.execute(text("DELETE FROM tenants WHERE slug = :s"), {"s": slug})
    admin_session.commit()
    t = Tenant(slug=slug, name=name)
    admin_session.add(t)
    admin_session.commit()
    admin_session.refresh(t)
    return t


@pytest.fixture
def tenant_a(admin_session):
    t = _make_tenant(admin_session, "alpha", "Alpha Test Tenant")
    yield t
    admin_session.rollback()
    admin_session.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": str(t.id)})
    admin_session.commit()


@pytest.fixture
def tenant_b(admin_session):
    t = _make_tenant(admin_session, "beta", "Beta Test Tenant")
    yield t
    admin_session.rollback()
    admin_session.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": str(t.id)})
    admin_session.commit()


@pytest.fixture
def sign_in_account(admin_session, tenant_a, app_user_session):
    setup = admin_session
    now = datetime.now(UTC)
    party = Party(
        tenant_id=tenant_a.id,
        party_type=PartyType.person,
        display_name="Sign In",
        email=f"signin-{uuid4().hex}@a.edu",
        is_active=True,
    )
    setup.add(party)
    setup.flush()
    setup.add(PartyPerson(party_id=party.id, first_name="Sign", last_name="In"))
    setup.flush()
    setup.add(
        AcademyAccountProfile(
            tenant_id=tenant_a.id, party_id=party.id, status="active", prefs={}, created_at=now, updated_at=now
        )
    )
    credential = UserCredential(
        tenant_id=tenant_a.id, party_id=party.id, password_hash=hash_password("chosen-password9")
    )
    setup.add(credential)
    setup.flush()
    setup.add(
        AcademyCredentialPolicy(
            tenant_id=tenant_a.id,
            credential_id=credential.id,
            email=party.email,
            failed_login_attempts=0,
            locked_until=None,
            lifecycle_version=0,
            reset_required=False,
            created_at=now,
            updated_at=now,
        )
    )
    setup.commit()
    db = app_user_session
    assert db.scalar(text("SELECT current_user")) == "app_user"
    db.execute(text("SELECT set_config('app.current_tenant', :tenant, true)"), {"tenant": str(tenant_a.id)})
    return db, tenant_a.id, party.id, party.email, credential.id


@pytest.fixture
def current_account(request):
    db, tid, pid, email, cid = request.getfixturevalue("sign_in_account")
    result = sign_in(db, tenant_id=tid, email=email, password="chosen-password9")
    assert result is not None
    db.commit()
    resolve_database_runtime().set_tenant(db, tid)
    assert db.scalar(text("SELECT current_user")) == "app_user"
    return SimpleNamespace(
        db=db,
        admin=request.getfixturevalue("admin_session"),
        tenant=request.getfixturevalue("tenant_a"),
        tid=tid,
        pid=pid,
        email=email,
        credential_id=cid,
        token=result.session_token,
        session_id=result.session_id,
        login_snapshot=result.account,
    )


def _person(db, *, tenant_id, email, first_name, last_name, status="active", is_active=True):
    now = datetime.now(UTC)
    party = Party(
        tenant_id=tenant_id,
        party_type=PartyType.person,
        email=email,
        display_name=f"{first_name} {last_name}",
        is_active=is_active,
    )
    db.add(party)
    db.flush()
    db.add(PartyPerson(party_id=party.id, first_name=first_name, last_name=last_name))
    db.flush()
    db.add(
        AcademyAccountProfile(
            tenant_id=tenant_id, party_id=party.id, status=status, prefs={}, created_at=now, updated_at=now
        )
    )
    db.flush()
    return decisions.InvitationAccountView(party.id, tenant_id, email, first_name, last_name, status, is_active)


def admission_admin_request(db, tenant, *, transport="api"):
    """Genuine canonical admin sign-in Request (ported legacy helper)."""
    resolve_database_runtime().set_tenant(db, tenant.id)
    person = _person(
        db,
        tenant_id=tenant.id,
        email=f"admission-admin-{uuid4().hex}@a.edu",
        first_name="Admission",
        last_name="Admin",
    )
    credential = UserCredential(
        tenant_id=tenant.id, party_id=person.id, password_hash=hash_password("chosen-password9")
    )
    db.add(credential)
    db.flush()
    now = datetime.now(UTC)
    db.add(
        AcademyCredentialPolicy(
            tenant_id=tenant.id,
            credential_id=credential.id,
            email=person.email,
            failed_login_attempts=0,
            lifecycle_version=0,
            reset_required=False,
            created_at=now,
            updated_at=now,
        )
    )
    role = db.scalars(select(Role).where(Role.tenant_id == tenant.id, Role.slug == "admin")).first()
    if role is None:
        role = Role(tenant_id=tenant.id, slug="admin", name="Admin")
        db.add(role)
        db.flush()
    db.add(PartyRoleGrant(tenant_id=tenant.id, party_id=person.id, role_id=role.id))
    db.flush()
    login = sign_in(db, tenant_id=tenant.id, email=person.email, password="chosen-password9")
    assert login is not None
    header = (
        (b"cookie", f"session={login.session_token}".encode())
        if transport == "web"
        else (b"authorization", f"Bearer {login.session_token}".encode())
    )
    return Request(
        {
            "type": "http",
            "scheme": "https",
            "server": ("academy.example", 443),
            "path": "/",
            "root_path": "",
            "query_string": b"",
            "headers": [header],
            "state": {"tenant": tenant},
        }
    )


def _cohort(admin_session, tenant, bank=None):
    c = Cohort(
        tenant_id=tenant.id,
        name="FA2",
        discipline="fiber",
        status="active",
        entrance_bank_id=(bank.id if bank else None),
    )
    admin_session.add(c)
    admin_session.flush()
    return c


def _applicant(admin_session, tenant, cohort, *, started=True):
    a = Applicant(
        tenant_id=tenant.id,
        email=f"a{uuid.uuid4().hex[:6]}@x.ex",
        first_name="A",
        last_name="B",
        status="applied",
        cohort_id=cohort.id,
        assessment_started_at=(datetime.now(UTC) - timedelta(minutes=15) if started else None),
    )
    admin_session.add(a)
    admin_session.flush()
    return a


def _stored(db, value):
    db.execute(
        delete(DomainSetting).where(
            DomainSetting.domain == "academy",
            DomainSetting.scope_kind == "platform",
            DomainSetting.key == KEY,
        )
    )
    db.add(
        DomainSetting(
            tenant_id=None, scope_kind="platform", domain="academy", key=KEY, value_text=value, value_json=None
        )
    )
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


def test_entrance_invitation_deadline_policy_strict(admin_session, monkeypatch):
    monkeypatch.setattr(settings_store.settings, KEY, 5)
    settings_store.set_many(admin_session, {KEY: "11"})
    first = policy.entrance_invitation_policy(admin_session)
    assert first.deadline_days == 11
    settings_store.set_many(admin_session, {KEY: "13"})
    assert policy.entrance_invitation_policy(admin_session).deadline_days == 13
    assert first.deadline_days == 11
    settings_store.delete(admin_session, KEY)
    assert policy.entrance_invitation_policy(admin_session).deadline_days == 5
    _stored(admin_session, "malformed")
    with pytest.raises(ValueError):
        policy.entrance_invitation_policy(admin_session)


@pytest.mark.parametrize("bad", [None, "malformed", "0", "-1", "true", "1.5"])
def test_actual_malformed_stored_policy_is_retained_and_refused(admin_session, bad):
    if bad is None:
        # Kernel storage forbids the NULL-valued row outright.
        with pytest.raises(IntegrityError):
            _stored(admin_session, None)
        admin_session.rollback()
        return
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
    prior = settings_store.get_all(a.admin).get(KEY)
    previous = (KEY in settings_store.get_all(a.admin), prior)

    def restore_global_setting():
        a.db.rollback()
        a.admin.rollback()
        if previous[0]:
            _stored(a.admin, previous[1])
        else:
            settings_store.delete(a.admin, KEY)
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
