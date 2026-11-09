"""Hosted-only genuine canonical lifecycle tests, authored but not run locally.

These retain the seven legacy reset/status scenarios. Invitations remain in the
untouched legacy test module, whose retired auth imports still block collection
until the coordinator performs the complete canonical integration. No fake
schema, proof port, runtime substitution or public status authority is supplied.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from time import monotonic, sleep
from uuid import uuid4

import pytest
from dotmac_kernel.audit import AuditEvent
from dotmac_kernel.audit_actions import AuditActionRegistry
from dotmac_kernel.models import AuthSession, Party, PartyPerson, PartyType, UserCredential
from dotmac_kernel.security import verify_password
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import sessionmaker

from app.models.account_token import AccountToken
from app.models.identity_extensions import AcademyAccountProfile, AcademyCredentialPolicy
from app.owner_composition import compose_migration_owner_declarations
from app.services import canonical_lifecycle as lifecycle
from app.services import credential_lifecycle as reset_adapter
from app.services.credential_authentication import sign_in
from app.services.credential_policy import PasswordResetTokenPolicy
from app.services.exceptions import BadRequestError
from app.services.security import hash_password, hash_token


@pytest.fixture(autouse=True)
def _audit_registry_installed(monkeypatch):
    # The registry is process-global and installed only by the app entry points
    # (create_app / bind_academy_database_runtime); install the composed one.
    registry = AuditActionRegistry.from_manifests(compose_migration_owner_declarations())
    monkeypatch.setattr("dotmac_kernel.audit_actions._active_registry", registry)


@pytest.fixture
def canonical_account(admin_session, tenant_a, app_user_session):
    setup = admin_session
    now = datetime.now(UTC)
    party = Party(
        tenant_id=tenant_a.id,
        party_type=PartyType.person,
        display_name="Canonical Lifecycle",
        email=f"lifecycle-{uuid4().hex}@a.edu",
        is_active=True,
    )
    setup.add(party)
    setup.flush()
    setup.add(PartyPerson(party_id=party.id, first_name="Canonical", last_name="Lifecycle"))
    setup.flush()
    setup.add(
        AcademyAccountProfile(
            tenant_id=tenant_a.id, party_id=party.id, status="active", prefs={}, created_at=now, updated_at=now
        )
    )
    credential = UserCredential(tenant_id=tenant_a.id, party_id=party.id, password_hash=hash_password("origpass1"))
    setup.add(credential)
    setup.flush()
    setup.add(
        AcademyCredentialPolicy(
            tenant_id=tenant_a.id,
            credential_id=credential.id,
            email=party.email,
            failed_login_attempts=0,
            locked_until=None,
            lifecycle_version=2,
            reset_required=False,
            created_at=now,
            updated_at=now,
        )
    )
    setup.commit()
    db = app_user_session
    assert db.get_bind().dialect.name == "postgresql"
    assert db.scalar(text("SELECT current_user")) == "app_user"
    _scope(db, tenant_a.id)
    return db, tenant_a.id, party.id, party.email, credential.id


def _scope(db, tid):
    db.execute(text("SELECT set_config('app.current_tenant', :tenant, true)"), {"tenant": str(tid)})


def _request(account, **kwargs):
    db, tid, _, email, _ = account
    return lifecycle.request_password_reset(db, tenant_id=tid, email=kwargs.pop("email", email), **kwargs)


def _reset(account, raw, **kwargs):
    db, tid, *_ = account
    return lifecycle.reset_password(
        db, tenant_id=tid, raw=raw, new_password=kwargs.pop("new_password", "brandnew9"), **kwargs
    )


def _policy(account):
    db, _, _, _, cid = account
    return db.get(AcademyCredentialPolicy, cid)


def _tokens(account):
    db, tid, pid, *_ = account
    return db.scalars(select(AccountToken).where(AccountToken.tenant_id == tid, AccountToken.person_id == pid)).all()


def _sessions(account):
    db, tid, pid, *_ = account
    return db.scalars(select(AuthSession).where(AuthSession.tenant_id == tid, AuthSession.party_id == pid)).all()


def _session(db, tid, pid, expiry, revoked_at=None):
    session = AuthSession(
        tenant_id=tid,
        party_id=pid,
        token_hash=hash_token(uuid4().hex),
        expires_at=expiry,
        revoked_at=revoked_at,
    )
    db.add(session)
    db.flush()
    return session


def test_password_reset_round_trip(canonical_account):
    db, tid, pid, email, cid = canonical_account
    root = db.get_transaction()
    raw = _request(canonical_account, email=f" {email.upper()} ")
    assert raw is not None
    token = _tokens(canonical_account)[0]
    assert token.token_hash == hash_token(raw) and token.token_hash != raw
    assert token.person_id == pid
    assert _reset(canonical_account, raw).id == pid
    credential = db.get(UserCredential, cid)
    assert verify_password("brandnew9", credential.password_hash)
    assert not verify_password("origpass1", credential.password_hash)
    assert db.get_transaction() is root
    assert db.scalar(text("SELECT current_setting('app.current_tenant')")) == str(tid)
    with pytest.raises(BadRequestError):
        _reset(canonical_account, raw, new_password="another9x")


def test_password_reset_unknown_email_returns_none(canonical_account):
    assert _request(canonical_account, email=f"unknown-{uuid4().hex}@a.edu") is None
    assert _tokens(canonical_account) == []


def test_password_reset_credentialless_invitee_returns_none(canonical_account):
    db, _, _, email, cid = canonical_account
    db.delete(_policy(canonical_account))
    db.delete(db.get(UserCredential, cid))
    db.flush()
    assert _request(canonical_account, email=f" {email.upper()} ") is None
    assert _tokens(canonical_account) == []


def test_password_reset_updates_all_credentials_for_person(canonical_account, monkeypatch):
    import dotmac_kernel.credential_lifecycle as owner

    db, tid, pid, _, cid = canonical_account
    extra = UserCredential(tenant_id=tid, party_id=pid, password_hash=hash_password("stale-password"))
    db.add(extra)
    db.flush()
    now = datetime.now(UTC)
    db.add(
        AcademyCredentialPolicy(
            tenant_id=tid,
            credential_id=extra.id,
            email=f"alias-{uuid4().hex}@a.edu",
            failed_login_attempts=4,
            locked_until=now + timedelta(hours=1),
            lifecycle_version=7,
            reset_required=True,
            created_at=now,
            updated_at=now,
        )
    )
    db.flush()
    calls = []
    original = owner.hash_password

    def counted(secret):
        calls.append(True)
        return original(secret)

    monkeypatch.setattr(owner, "hash_password", counted)
    _reset(canonical_account, _request(canonical_account))
    credentials = [db.get(UserCredential, cid), extra]
    assert len(credentials) == 2
    assert len(calls) == 1
    assert len({row.password_hash for row in credentials}) == 1
    assert all(verify_password("brandnew9", row.password_hash) for row in credentials)
    assert all(not verify_password("origpass1", row.password_hash) for row in credentials)
    assert all(not verify_password("stale-password", row.password_hash) for row in credentials)
    policies = [db.get(AcademyCredentialPolicy, row.id) for row in credentials]
    assert sorted(row.lifecycle_version for row in policies) == [3, 8]
    assert all(
        not row.reset_required and row.failed_login_attempts == 0 and row.locked_until is None for row in policies
    )


def test_reset_rejects_expired_token(canonical_account):
    raw = _request(canonical_account, now=datetime.now(UTC) - timedelta(days=365))
    with pytest.raises(BadRequestError):
        _reset(canonical_account, raw)
    assert _tokens(canonical_account)[0].used_at is None


def test_reset_clears_lockout_revokes_sessions_and_invalidates_other_tokens(canonical_account):
    db, tid, pid, email, _ = canonical_account
    now = datetime.now(UTC)
    policy = _policy(canonical_account)
    policy.failed_login_attempts = 5
    policy.locked_until = now + timedelta(minutes=15)
    sessions = [
        _session(db, tid, pid, now + timedelta(hours=1)),
        _session(db, tid, pid, now - timedelta(hours=1)),
        _session(db, tid, pid, now - timedelta(hours=1), now - timedelta(days=1)),
    ]
    prior_revocation = sessions[2].revoked_at
    stale = _request(canonical_account, email=f" {email.upper()} ")
    current = _request(canonical_account)
    assert stale is not None and current is not None
    assert all(token.used_at is None for token in _tokens(canonical_account))
    _reset(canonical_account, current, new_password="unlocked-password")
    assert policy.failed_login_attempts == 0
    assert policy.locked_until is None
    assert sessions and all(session.revoked_at is not None for session in sessions)
    assert sessions[2].revoked_at == prior_revocation
    tokens = _tokens(canonical_account)
    assert len(tokens) == 2
    assert all(token.used_at is not None for token in tokens)
    with pytest.raises(BadRequestError):
        _reset(canonical_account, stale, new_password="another-password")


def test_suspended_account_cannot_authenticate(canonical_account):
    db, tid, pid, email, _ = canonical_account
    first = sign_in(db, tenant_id=tid, email=email, password="origpass1")
    assert first is not None
    party = db.get(Party, pid)
    activity = party.is_active
    profile = lifecycle.set_account_status(db, tenant_id=tid, person_id=pid, status="suspended")
    assert profile.status == "suspended"
    assert sign_in(db, tenant_id=tid, email=email, password="origpass1") is None
    assert all(row.revoked_at is not None for row in _sessions(canonical_account))
    old_revocation = db.get(AuthSession, first.session_id).revoked_at
    count = len(_sessions(canonical_account))
    lifecycle.set_account_status(db, tenant_id=tid, person_id=pid, status="active")
    assert len(_sessions(canonical_account)) == count
    assert db.get(AuthSession, first.session_id).revoked_at == old_revocation
    assert party.is_active is activity
    assert sign_in(db, tenant_id=tid, email=email, password="origpass1") is not None


def test_status_revokes_expired_sessions_and_preserves_prior_stamps(canonical_account):
    db, tid, pid, *_ = canonical_account
    now = datetime.now(UTC)
    expired = _session(db, tid, pid, now - timedelta(days=1))
    prior = _session(db, tid, pid, now - timedelta(days=1), now - timedelta(days=2))
    lifecycle.set_account_status(db, tenant_id=tid, person_id=pid, status="suspended", now=now)
    assert expired.revoked_at == now
    assert prior.revoked_at == now - timedelta(days=2)
    assert db.get(AcademyAccountProfile, pid).updated_at == now


@pytest.mark.parametrize(
    "status,message", [("unknown", "invalid account status: unknown"), ("active", "person not found")]
)
def test_status_invalid_and_missing_person_preserve_legacy_errors(canonical_account, status, message):
    db, tid, *_ = canonical_account
    with pytest.raises(BadRequestError, match=message):
        lifecycle.set_account_status(db, tenant_id=tid, person_id=uuid4(), status=status)


def test_stale_identity_maps_never_supply_request_or_status_state(canonical_account):
    db, tid, pid, email, _ = canonical_account
    party = db.get(Party, pid)
    profile = db.get(AcademyAccountProfile, pid)
    db.execute(
        update(Party)
        .where(Party.id == pid)
        .values(email=f"changed-{uuid4().hex}@a.edu")
        .execution_options(synchronize_session=False)
    )
    db.execute(
        update(AcademyAccountProfile)
        .where(AcademyAccountProfile.party_id == pid)
        .values(status="suspended")
        .execution_options(synchronize_session=False)
    )
    assert party.email == email and profile.status == "active"
    assert _request(canonical_account) is None
    lifecycle.set_account_status(db, tenant_id=tid, person_id=pid, status="active")
    assert party.email != email and profile.status == "active"


@pytest.mark.parametrize("missing", ["person", "profile"])
def test_request_requires_genuine_person_and_profile(canonical_account, missing):
    db, tid, pid, *_ = canonical_account
    model = PartyPerson if missing == "person" else AcademyAccountProfile
    db.delete(db.get(model, pid))
    db.flush()
    assert _request(canonical_account) is None
    with pytest.raises(BadRequestError, match="person not found"):
        lifecycle.set_account_status(db, tenant_id=tid, person_id=pid, status="suspended")


def test_exact_expiration_is_valid_and_expired_consumption_refuses(canonical_account, monkeypatch):
    db, _, _, _, cid = canonical_account
    now = datetime.now(UTC)
    raw = _request(canonical_account)
    token = _tokens(canonical_account)[0]
    token.expires_at = now
    db.flush()
    _reset(canonical_account, raw, now=now)
    assert token.used_at == now
    raw = _request(canonical_account)
    token = next(row for row in _tokens(canonical_account) if row.used_at is None)
    token.expires_at = now + timedelta(seconds=1)
    db.flush()
    original = reset_adapter._ResetPorts.prove

    def deterministic_proof(self):
        # Real proof and locked token; only the clock changes, never authority.
        self.clock = lambda: now
        return original(self)

    consume = reset_adapter._ResetPorts.consume

    def advance_at_consumption(self, authorization):
        self.clock = lambda: now + timedelta(seconds=2)
        return consume(self, authorization)

    monkeypatch.setattr(reset_adapter._ResetPorts, "prove", deterministic_proof)
    monkeypatch.setattr(reset_adapter._ResetPorts, "consume", advance_at_consumption)
    with pytest.raises(BadRequestError):
        _reset(canonical_account, raw)
    assert token.used_at is None
    assert verify_password("brandnew9", db.get(UserCredential, cid).password_hash)


@pytest.mark.parametrize("fixed", [None, datetime(2026, 10, 18, tzinfo=UTC)])
def test_reset_timestamp_accessor_requires_actual_post_lock_observation(canonical_account, fixed):
    db, tid, *_ = canonical_account
    issued_at = fixed if fixed is not None else datetime.now(UTC)
    raw = _request(canonical_account, now=issued_at)
    ports = reset_adapter._ResetPorts(db, tenant_id=tid, raw=raw, now=fixed)
    with pytest.raises(reset_adapter.CredentialLifecycleError, match="clock has not been observed"):
        _ = ports.now
    authorization = ports.prove()
    assert ports.now == authorization.proven_at
    assert ports.now == fixed if fixed is not None else ports.now >= issued_at


@pytest.mark.parametrize("step", ["consume", "write_hash", "revoke_sessions", "invalidate_siblings"])
def test_caught_failure_outer_commit_has_no_reset_effects(canonical_account, monkeypatch, step):
    db, tid, pid, _, cid = canonical_account
    raw = _request(canonical_account)
    original_hash = db.get(UserCredential, cid).password_hash
    original_version = _policy(canonical_account).lifecycle_version
    session = _session(db, tid, pid, datetime.now(UTC) - timedelta(days=1))
    db.commit()
    _scope(db, tid)
    root = db.get_transaction()
    method = getattr(reset_adapter._ResetPorts, step)

    def fail_after(self, *args, **kwargs):
        method(self, *args, **kwargs)
        raise reset_adapter.CredentialCohortDrift("injected expected failure")

    monkeypatch.setattr(reset_adapter._ResetPorts, step, fail_after)
    with pytest.raises(BadRequestError):
        _reset(canonical_account, raw)
    assert db.is_active and db.get_transaction() is root
    db.commit()
    _scope(db, tid)
    db.expire_all()
    assert all(row.used_at is None for row in _tokens(canonical_account))
    assert db.get(UserCredential, cid).password_hash == original_hash
    assert _policy(canonical_account).lifecycle_version == original_version
    assert db.get(AuthSession, session.id).revoked_at is None
    assert not db.scalars(select(AuditEvent).where(AuditEvent.tenant_id == tid)).all()


def test_caught_status_conflict_outer_commit_has_no_effects(canonical_account, monkeypatch):
    db, tid, pid, *_ = canonical_account
    session = _session(db, tid, pid, datetime.now(UTC) - timedelta(days=1))
    sid = session.id
    db.commit()
    _scope(db, tid)
    profile = db.get(AcademyAccountProfile, pid)
    prior_stamp = profile.updated_at
    root = db.get_transaction()
    original = db.flush
    injected = False

    def fail_after_flush(*args, **kwargs):
        nonlocal injected
        original(*args, **kwargs)
        if profile.status == "suspended" and not injected:
            injected = True
            raise IntegrityError("injected status conflict", {}, RuntimeError("injected"))

    monkeypatch.setattr(db, "flush", fail_after_flush)
    with pytest.raises(BadRequestError, match="account lifecycle conflict"):
        lifecycle.set_account_status(db, tenant_id=tid, person_id=pid, status="suspended")
    monkeypatch.setattr(db, "flush", original)
    assert injected and db.is_active and db.get_transaction() is root
    db.commit()
    _scope(db, tid)
    db.expire_all()
    assert profile.status == "active" and profile.updated_at == prior_stamp
    assert db.get(AuthSession, sid).revoked_at is None


def _wait_for_lock(db, backend_pid):
    deadline = monotonic() + 10
    while not db.scalar(
        text("SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = :pid"), {"pid": backend_pid}
    ):
        assert monotonic() < deadline, "contender never waited on the actual canonical row lock"
        sleep(0.01)


@pytest.mark.parametrize("writer", ["reset", "status"])
@pytest.mark.parametrize("first", ["login", "writer"])
def test_actual_login_and_reset_or_status_both_race_orders(canonical_account, writer, first):
    """Both contenders call actual adapters; this is no prospective writer stub."""
    db, tid, pid, email, _ = canonical_account
    raw = _request(canonical_account)
    db.commit()
    _scope(db, tid)
    factory = sessionmaker(bind=db.get_bind(), autoflush=False)
    locked, started, release = Event(), Event(), Event()
    contender_pid, result = [], {}

    def work(kind, leader):
        with factory() as session:
            assert session.scalar(text("SELECT current_user")) == "app_user"
            _scope(session, tid)
            if leader:
                session.scalars(select(Party).where(Party.tenant_id == tid, Party.id == pid).with_for_update()).one()
                locked.set()
                assert release.wait(10)
            else:
                assert locked.wait(10)
                contender_pid.append(session.scalar(text("SELECT pg_backend_pid()")))
                started.set()
            if kind == "login":
                result["login"] = sign_in(session, tenant_id=tid, email=email, password="origpass1")
            elif writer == "reset":
                lifecycle.reset_password(session, tenant_id=tid, raw=raw, new_password="brandnew9")
            else:
                lifecycle.set_account_status(session, tenant_id=tid, person_id=pid, status="suspended")
            session.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = pool.submit(work, first, True)
        try:
            assert locked.wait(10)
            follower = pool.submit(work, "writer" if first == "login" else "login", False)
            assert started.wait(10)
            _wait_for_lock(db, contender_pid[0])
        finally:
            release.set()
        leader.result(timeout=20)
        follower.result(timeout=20)
    db.expire_all()
    if first == "writer":
        assert result["login"] is None
    else:
        assert result["login"] is not None
        assert db.get(AuthSession, result["login"].session_id).revoked_at is not None


def test_proof_expiring_during_real_principal_lock_wait_refuses(canonical_account):
    db, tid, pid, _, cid = canonical_account
    raw = _request(canonical_account)
    token = _tokens(canonical_account)[0]
    expires_at = datetime.now(UTC) + timedelta(seconds=2)
    token.expires_at = expires_at
    db.commit()
    _scope(db, tid)
    factory = sessionmaker(bind=db.get_bind(), autoflush=False)
    started, release = Event(), Event()
    contender_pid = []

    def consume():
        with factory() as session:
            _scope(session, tid)
            contender_pid.append(session.scalar(text("SELECT pg_backend_pid()")))
            started.set()
            with pytest.raises(BadRequestError):
                lifecycle.reset_password(session, tenant_id=tid, raw=raw, new_password="brandnew9")
            session.commit()

    with factory() as blocker, ThreadPoolExecutor(max_workers=1) as pool:
        _scope(blocker, tid)
        blocker.scalars(select(Party).where(Party.tenant_id == tid, Party.id == pid).with_for_update()).one()
        future = pool.submit(consume)
        try:
            assert started.wait(10)
            _wait_for_lock(db, contender_pid[0])
            while datetime.now(UTC) <= expires_at:
                release.wait(0.01)
        finally:
            blocker.commit()
        future.result(timeout=20)
    db.expire_all()
    assert db.get(AccountToken, token.id).used_at is None
    assert verify_password("origpass1", db.get(UserCredential, cid).password_hash)


def test_reset_naturally_holds_principal_before_waiting_for_subtype(canonical_account):
    """Actual reset acquires Party itself; no leader prelocks its principal."""
    db, tid, pid, _, cid = canonical_account
    raw = _request(canonical_account)
    db.commit()
    _scope(db, tid)
    factory = sessionmaker(bind=db.get_bind(), autoflush=False)
    started = Event()
    reset_pid = []

    def reset():
        with factory() as session:
            assert session.scalar(text("SELECT current_user")) == "app_user"
            _scope(session, tid)
            reset_pid.append(session.scalar(text("SELECT pg_backend_pid()")))
            started.set()
            lifecycle.reset_password(session, tenant_id=tid, raw=raw, new_password="brandnew9")
            session.commit()

    with factory() as subtype_holder, ThreadPoolExecutor(max_workers=1) as pool:
        assert subtype_holder.scalar(text("SELECT current_user")) == "app_user"
        _scope(subtype_holder, tid)
        holder_pid = subtype_holder.scalar(text("SELECT pg_backend_pid()"))
        subtype_holder.scalars(select(PartyPerson).where(PartyPerson.party_id == pid).with_for_update()).one()
        future = pool.submit(reset)
        try:
            assert started.wait(10)
            _wait_for_lock(db, reset_pid[0])
            assert holder_pid in db.scalar(text("SELECT pg_blocking_pids(:pid)"), {"pid": reset_pid[0]})
            # A third app_user transaction cannot acquire Party while reset is
            # blocked on the genuine subtype. The subtype holder owns no Party
            # lock, and this probe never supplies reset's principal lock.
            with factory() as probe:
                assert probe.scalar(text("SELECT current_user")) == "app_user"
                _scope(probe, tid)
                with pytest.raises(OperationalError) as conflict:
                    probe.scalars(
                        select(Party).where(Party.tenant_id == tid, Party.id == pid).with_for_update(nowait=True)
                    ).one()
                assert conflict.value.orig.sqlstate == "55P03"
                probe.rollback()
        finally:
            subtype_holder.commit()
        future.result(timeout=20)
    db.expire_all()
    assert all(row.used_at is not None for row in _tokens(canonical_account))
    assert verify_password("brandnew9", db.get(UserCredential, cid).password_hash)


@pytest.mark.parametrize("change", ["email", "credential"])
def test_request_rechecks_actual_account_after_principal_wait(canonical_account, monkeypatch, change):
    db, tid, pid, email, cid = canonical_account
    db.commit()
    _scope(db, tid)
    factory = sessionmaker(bind=db.get_bind(), autoflush=False)
    identified = Event()
    contender_pid, result = [], []
    original = lifecycle.canonical_party_for_email

    def identify(*args, **kwargs):
        party = original(*args, **kwargs)
        identified.set()
        return party

    monkeypatch.setattr(lifecycle, "canonical_party_for_email", identify)

    def request():
        with factory() as session:
            _scope(session, tid)
            contender_pid.append(session.scalar(text("SELECT pg_backend_pid()")))
            result.append(lifecycle.request_password_reset(session, tenant_id=tid, email=email))
            session.commit()

    with factory() as blocker, ThreadPoolExecutor(max_workers=1) as pool:
        _scope(blocker, tid)
        party = blocker.scalars(select(Party).where(Party.tenant_id == tid, Party.id == pid).with_for_update()).one()
        future = pool.submit(request)
        try:
            assert identified.wait(10)
            _wait_for_lock(db, contender_pid[0])
            if change == "email":
                party.email = f"replacement-{uuid4().hex}@a.edu"
            else:
                blocker.delete(blocker.get(AcademyCredentialPolicy, cid))
                blocker.delete(blocker.get(UserCredential, cid))
        finally:
            blocker.commit()
        future.result(timeout=20)
    assert result == [None]
    assert _tokens(canonical_account) == []


def test_status_timestamp_and_revocations_follow_blocking_session_lock(canonical_account):
    db, tid, pid, *_ = canonical_account
    session = _session(db, tid, pid, datetime.now(UTC) - timedelta(days=1))
    sid = session.id
    db.commit()
    _scope(db, tid)
    factory = sessionmaker(bind=db.get_bind(), autoflush=False)
    started = Event()
    contender_pid = []

    def suspend():
        with factory() as session:
            _scope(session, tid)
            contender_pid.append(session.scalar(text("SELECT pg_backend_pid()")))
            started.set()
            lifecycle.set_account_status(session, tenant_id=tid, person_id=pid, status="suspended")
            session.commit()

    with factory() as blocker, ThreadPoolExecutor(max_workers=1) as pool:
        _scope(blocker, tid)
        # A retained pre-cohort session-row writer forces the last blocking lock.
        # This canary proves timestamp placement, never grants that writer authority.
        blocker.scalars(
            select(AuthSession).where(AuthSession.tenant_id == tid, AuthSession.id == sid).with_for_update()
        ).one()
        future = pool.submit(suspend)
        try:
            assert started.wait(10)
            _wait_for_lock(db, contender_pid[0])
            released_at = datetime.now(UTC)
        finally:
            blocker.commit()
        future.result(timeout=20)
    db.expire_all()
    profile = db.get(AcademyAccountProfile, pid)
    assert profile.updated_at >= released_at
    assert db.get(AuthSession, sid).revoked_at == profile.updated_at


@pytest.mark.parametrize("minutes", [0, 0.00000001, 0.5, 120])
def test_required_token_policy_native_rounding_and_calendar_bounds(minutes):
    now = datetime(2026, 10, 18, tzinfo=UTC)
    policy = PasswordResetTokenPolicy(minutes)
    assert policy.deadline(now) == now + timedelta(minutes=minutes)
    if minutes >= 0.5:
        with pytest.raises(ValueError, match="deadline is not representable"):
            policy.deadline(datetime.max.replace(tzinfo=UTC))


@pytest.mark.parametrize("value", [True, False, None, -1, float("nan"), float("inf"), 1e300])
def test_required_token_policy_refuses_invalid_configuration(value):
    with pytest.raises(ValueError):
        PasswordResetTokenPolicy(value)
