"""Hosted PostgreSQL canaries; canonical assembly and installed owner required."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from dotmac_kernel.models import AuthSession, Party, PartyPerson, PartyType, UserCredential
from sqlalchemy import select, text

from app.models.identity_extensions import AcademyAccountProfile, AcademyCredentialPolicy
from app.services import credential_authentication as adapter
from app.services.security import hash_password, hash_token


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
    policy = AcademyCredentialPolicy(
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
    setup.add(policy)
    setup.commit()
    db = app_user_session
    assert db.get_bind().dialect.name == "postgresql"
    assert db.scalar(text("SELECT current_user")) == "app_user"
    db.execute(text("SELECT set_config('app.current_tenant', :tenant, true)"), {"tenant": str(tenant_a.id)})
    return db, tenant_a.id, party.id, party.email, credential.id


def _sign(account, password="chosen-password9"):
    db, tid, _, email, _ = account
    return adapter.sign_in(db, tenant_id=tid, email=email, password=password)


def _policy(account):
    db, tid, _, _, cid = account
    return db.scalars(
        select(AcademyCredentialPolicy).where(
            AcademyCredentialPolicy.tenant_id == tid, AcademyCredentialPolicy.credential_id == cid
        )
    ).one()


def test_flushed_canonical_session_projection_and_legacy_hash_unchanged(sign_in_account):
    db, tid, pid, email, cid = sign_in_account
    credential = db.get(UserCredential, cid)
    old_hash = credential.password_hash
    root = db.get_transaction()
    result = _sign(sign_in_account)
    assert result.account.id == pid and result.account.tenant_id == tid
    assert result.account.email == email and result.account.first_name == "Sign"
    session = db.get(AuthSession, result.session_id)
    assert session.party_id == pid and session.tenant_id == tid
    assert session.token_hash == hash_token(result.session_token)
    assert result.session_token not in repr(result)
    assert credential.password_hash == old_hash
    assert db.get_transaction() is root
    assert db.scalar(text("SELECT current_setting('app.current_tenant')")) == str(tid)
    with pytest.raises((AttributeError, TypeError)):
        result.account.email = "changed@a.edu"


def test_ordinary_bad_attempt_commits_selected_counter(sign_in_account):
    db, tid, *_ = sign_in_account
    prior_stamp = _policy(sign_in_account).updated_at
    assert _sign(sign_in_account, "wrong") is None
    assert _policy(sign_in_account).failed_login_attempts == 1
    assert _policy(sign_in_account).updated_at > prior_stamp
    db.commit()
    db.execute(text("SELECT set_config('app.current_tenant', :tenant, true)"), {"tenant": str(tid)})
    db.expire_all()
    assert _policy(sign_in_account).failed_login_attempts == 1
    assert not db.scalars(select(AuthSession).where(AuthSession.tenant_id == tid)).all()


@pytest.mark.parametrize("restriction", ["suspended", "reset", "inactive", "missing_profile", "missing_policy"])
def test_restrictions_never_issue_session(sign_in_account, restriction):
    db, tid, pid, _, _ = sign_in_account
    policy = _policy(sign_in_account)
    policy.failed_login_attempts = 2
    if restriction == "suspended":
        db.get(AcademyAccountProfile, pid).status = "suspended"
    elif restriction == "reset":
        policy.reset_required = True
    elif restriction == "inactive":
        db.get(Party, pid).is_active = False
    elif restriction == "missing_profile":
        db.delete(db.get(AcademyAccountProfile, pid))
    else:
        db.delete(policy)
    db.flush()
    assert _sign(sign_in_account) is None
    assert not db.scalars(select(AuthSession).where(AuthSession.tenant_id == tid)).all()
    if restriction == "suspended":
        assert policy.failed_login_attempts == 0 and policy.locked_until is None


def test_logout_exact_session_preserves_prior_stamp(sign_in_account):
    from app.services.web_auth import revoke_session

    db, tid, *_ = sign_in_account
    first, second = _sign(sign_in_account), _sign(sign_in_account)
    revoke_session(db, tid, first.session_token)
    stamp = db.get(AuthSession, first.session_id).revoked_at
    revoke_session(db, tid, first.session_token)
    assert db.get(AuthSession, first.session_id).revoked_at == stamp
    assert db.get(AuthSession, second.session_id).revoked_at is None


@pytest.mark.parametrize("flush_number", [1, 2, 3])
def test_fault_at_each_owned_flush_recovers_savepoint_root_and_guc(sign_in_account, monkeypatch, flush_number):
    db, tid, *_ = sign_in_account
    policy = _policy(sign_in_account)
    policy.failed_login_attempts = 2
    db.flush()
    root = db.get_transaction()
    original = db.flush
    calls = 0
    begun = False
    locked_operation = adapter._sign_in_locked

    def inside(*args, **kwargs):
        nonlocal begun
        begun = True
        return locked_operation(*args, **kwargs)

    monkeypatch.setattr(adapter, "_sign_in_locked", inside)

    def fault(*args, **kwargs):
        nonlocal calls
        result = original(*args, **kwargs)
        if begun:
            calls += 1
        if begun and calls == flush_number:
            raise RuntimeError("synthetic flush fault")
        return result

    monkeypatch.setattr(db, "flush", fault)
    with pytest.raises(RuntimeError, match="synthetic flush fault"):
        _sign(sign_in_account)
    monkeypatch.setattr(db, "flush", original)
    assert db.is_active and db.get_transaction() is root
    assert db.scalar(text("SELECT current_setting('app.current_tenant')")) == str(tid)
    db.expire_all()
    assert _policy(sign_in_account).failed_login_attempts == 2
    assert not db.scalars(select(AuthSession).where(AuthSession.tenant_id == tid)).all()


def test_threshold_nondefault_and_lockout_precedes_owner(sign_in_account, monkeypatch):
    from app.services.authentication_policy import AuthenticationPolicy

    monkeypatch.setattr(adapter, "authentication_policy", lambda db: AuthenticationPolicy(2, 7))
    assert _sign(sign_in_account, "wrong") is None
    assert _policy(sign_in_account).locked_until is None
    assert _sign(sign_in_account, "wrong") is None
    policy = _policy(sign_in_account)
    assert policy.failed_login_attempts == 2
    assert 6 * 60 < (policy.locked_until - datetime.now(UTC)).total_seconds() <= 7 * 60

    def forbidden(*args, **kwargs):
        raise AssertionError("locked account reached password verification")

    monkeypatch.setattr(adapter.CredentialLifecycle, "verify", forbidden)
    assert _sign(sign_in_account) is None
    assert policy.failed_login_attempts == 2


def test_multiplicity_uses_actual_unordered_selected_counter_and_compatible_alias(sign_in_account):
    db, tid, pid, email, cid = sign_in_account
    extra = UserCredential(tenant_id=tid, party_id=pid, password_hash=hash_password("chosen-password9"))
    db.add(extra)
    db.flush()
    now = datetime.now(UTC)
    db.add(
        AcademyCredentialPolicy(
            tenant_id=tid,
            credential_id=extra.id,
            email=f"alias-{uuid4().hex}@a.edu",
            failed_login_attempts=8,
            locked_until=None,
            lifecycle_version=7,
            reset_required=False,
            created_at=now,
            updated_at=now,
        )
    )
    db.flush()
    selected = db.scalars(
        select(UserCredential).where(UserCredential.tenant_id == tid, UserCredential.party_id == pid)
    ).first()
    before = {
        row.credential_id: row.failed_login_attempts
        for row in db.scalars(select(AcademyCredentialPolicy).where(AcademyCredentialPolicy.tenant_id == tid))
    }
    assert _sign(sign_in_account, "wrong") is None
    after = {
        row.credential_id: row.failed_login_attempts
        for row in db.scalars(select(AcademyCredentialPolicy).where(AcademyCredentialPolicy.tenant_id == tid))
    }
    assert after[selected.id] == before[selected.id] + 1
    assert all(after[key] == value for key, value in before.items() if key != selected.id)
    for row in db.scalars(select(AcademyCredentialPolicy).where(AcademyCredentialPolicy.tenant_id == tid)):
        row.locked_until = None
    db.flush()
    assert _sign(sign_in_account) is not None
    assert _policy(sign_in_account).email == email


def test_wrong_tenant_never_resolves_account(sign_in_account, tenant_b):
    db, _, _, email, _ = sign_in_account
    assert adapter.sign_in(db, tenant_id=tenant_b.id, email=email, password="chosen-password9") is None


def test_stale_identity_map_is_refreshed(sign_in_account):
    from sqlalchemy import update

    db, tid, pid, *_ = sign_in_account
    profile = db.get(AcademyAccountProfile, pid)
    assert profile.status == "active"
    db.execute(
        update(AcademyAccountProfile)
        .where(AcademyAccountProfile.tenant_id == tid, AcademyAccountProfile.party_id == pid)
        .values(status="suspended")
        .execution_options(synchronize_session=False)
    )
    assert profile.status == "active"
    assert _sign(sign_in_account) is None
    assert profile.status == "suspended"


@pytest.mark.parametrize("writer", ["reset", "status", "revocation"])
@pytest.mark.parametrize("first", ["login", "writer"])
def test_principal_protocol_both_race_orders(sign_in_account, writer, first):
    """Real reset/logout adapters; status is a proposed writer protocol canary.

    This does not qualify unconverted production status/grant/guard writers.
    """
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from sqlalchemy.orm import sessionmaker

    from app.models.account_token import AccountToken
    from app.services.credential_lifecycle import complete_password_reset
    from app.services.credential_policy import AcademyPasswordPolicy
    from app.services.web_auth import revoke_session

    db, tid, pid, email, _ = sign_in_account
    existing = _sign(sign_in_account)
    raw = uuid4().hex + uuid4().hex
    from datetime import timedelta

    db.add(
        AccountToken(
            tenant_id=tid,
            person_id=pid,
            kind="password_reset",
            token_hash=hash_token(raw),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    db.commit()
    SessionLocal = sessionmaker(bind=db.get_bind(), autoflush=False)
    locked, contender_started, release = Event(), Event(), Event()
    results = {}
    contender_pid = []

    def work(kind, leader):
        with SessionLocal() as session:
            assert session.scalar(text("SELECT current_user")) == "app_user"
            session.execute(text("SELECT set_config('app.current_tenant', :tenant, true)"), {"tenant": str(tid)})
            if leader:
                session.scalars(select(Party).where(Party.tenant_id == tid, Party.id == pid).with_for_update()).one()
                locked.set()
                assert release.wait(10)
            else:
                assert locked.wait(10)
                contender_pid.append(session.scalar(text("SELECT pg_backend_pid()")))
                contender_started.set()
            if kind == "login":
                results[kind] = adapter.sign_in(session, tenant_id=tid, email=email, password="chosen-password9")
            elif writer == "reset":
                complete_password_reset(
                    session,
                    tenant_id=tid,
                    raw=raw,
                    new_password="replacement9",
                    password_policy=AcademyPasswordPolicy(minimum_length=8),
                )
            elif writer == "revocation":
                revoke_session(session, tid, existing.session_token)
            else:
                session.scalars(select(Party).where(Party.tenant_id == tid, Party.id == pid).with_for_update()).one()
                session.get(AcademyAccountProfile, pid).status = "suspended"
                session.flush()
            session.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = pool.submit(work, first, True)
        assert locked.wait(10)
        follower = pool.submit(work, "writer" if first == "login" else "login", False)
        assert contender_started.wait(10)
        from time import monotonic, sleep

        deadline = monotonic() + 10
        while not db.scalar(
            text("SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = :pid"), {"pid": contender_pid[0]}
        ):
            assert monotonic() < deadline, "contender never waited for the genuine principal row lock"
            sleep(0.01)
        release.set()
        leader.result(timeout=20)
        follower.result(timeout=20)
    db.execute(text("SELECT set_config('app.current_tenant', :tenant, true)"), {"tenant": str(tid)})
    db.expire_all()
    result = results["login"]
    if first == "writer" and writer in ("reset", "status"):
        assert result is None
    else:
        assert result is not None
        issued = db.get(AuthSession, result.session_id)
        assert (issued.revoked_at is not None) == (writer == "reset" and first == "login")
    if writer == "revocation":
        assert db.get(AuthSession, existing.session_id).revoked_at is not None


def test_alias_conflict_rolls_back_clear_and_session(sign_in_account):
    db, tid, pid, email, _ = sign_in_account
    policy = _policy(sign_in_account)
    policy.failed_login_attempts = 3
    policy.email = f"drift-{uuid4().hex}@a.edu"
    other = Party(
        tenant_id=tid,
        party_type=PartyType.person,
        display_name="Other",
        email=f"other-{uuid4().hex}@a.edu",
        is_active=True,
    )
    db.add(other)
    db.flush()
    db.add(PartyPerson(party_id=other.id, first_name="Other", last_name="Subject"))
    db.flush()
    credential = UserCredential(tenant_id=tid, party_id=other.id, password_hash=hash_password("other-password"))
    db.add(credential)
    db.flush()
    now = datetime.now(UTC)
    db.add(
        AcademyCredentialPolicy(
            tenant_id=tid,
            credential_id=credential.id,
            email=email,
            failed_login_attempts=0,
            locked_until=None,
            lifecycle_version=0,
            reset_required=False,
            created_at=now,
            updated_at=now,
        )
    )
    db.flush()
    root = db.get_transaction()
    assert _sign(sign_in_account) is None
    assert db.is_active and db.get_transaction() is root
    db.expire_all()
    assert _policy(sign_in_account).failed_login_attempts == 3
    assert not db.scalars(select(AuthSession).where(AuthSession.tenant_id == tid, AuthSession.party_id == pid)).all()


@pytest.mark.parametrize("change", ["hash", "reset", "inactive", "lock"])
def test_stale_credential_and_principal_state_never_authorizes(sign_in_account, change):
    from datetime import timedelta

    from sqlalchemy import update

    db, tid, pid, _, cid = sign_in_account
    credential = db.get(UserCredential, cid)
    policy = _policy(sign_in_account)
    party = db.get(Party, pid)
    assert credential and policy and party
    if change == "hash":
        statement = (
            update(UserCredential)
            .where(UserCredential.tenant_id == tid, UserCredential.id == cid)
            .values(password_hash=hash_password("replacement9"))
        )
    elif change == "inactive":
        statement = update(Party).where(Party.tenant_id == tid, Party.id == pid).values(is_active=False)
    else:
        values = (
            {"reset_required": True} if change == "reset" else {"locked_until": datetime.now(UTC) + timedelta(hours=1)}
        )
        statement = (
            update(AcademyCredentialPolicy)
            .where(AcademyCredentialPolicy.tenant_id == tid, AcademyCredentialPolicy.credential_id == cid)
            .values(**values)
        )
    db.execute(statement.execution_options(synchronize_session=False))
    assert _sign(sign_in_account) is None
    assert not db.scalars(select(AuthSession).where(AuthSession.tenant_id == tid, AuthSession.party_id == pid)).all()


def test_missing_genuine_person_subtype_refuses(sign_in_account):
    db, tid, pid, *_ = sign_in_account
    db.delete(db.get(PartyPerson, pid))
    db.flush()
    assert _sign(sign_in_account) is None
    assert not db.scalars(select(AuthSession).where(AuthSession.tenant_id == tid)).all()


def test_owner_current_hash_password_also_signs_in_without_hash_write(sign_in_account):
    from dotmac_kernel.security import hash_password as owner_hash

    db, _, _, _, cid = sign_in_account
    credential = db.get(UserCredential, cid)
    credential.password_hash = owner_hash("chosen-password9")
    db.flush()
    original = credential.password_hash
    assert _sign(sign_in_account) is not None
    assert credential.password_hash == original
