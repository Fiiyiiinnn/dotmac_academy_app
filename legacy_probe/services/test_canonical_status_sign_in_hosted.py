"""Authored hosted PostgreSQL actual status-owner/sign-in races; NOT run locally.

The trusted internal status entry is intentional: HTTP Admin/cookie policy has
its separate request-bound canaries. Both race effects below invoke the actual
services and installed public savepoint/storage owners, never a proposed direct
profile/session mutation. Fixture setup alone creates historical session rows.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from time import monotonic, sleep
from types import SimpleNamespace
from uuid import uuid4

import pytest
from dotmac_kernel.models import AuthSession, Party, PartyPerson, PartyType
from dotmac_kernel.session_runtime import resolve_database_runtime
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from app.models.identity_extensions import AcademyAccountProfile
from app.services import canonical_lifecycle, credential_authentication, web_auth
from app.services.security import hash_token
from legacy_probe.services.test_canonical_sign_in import sign_in_account

assert sign_in_account  # Register the genuine fixture, not a replacement account.


def _scope(db, tenant_id):
    resolve_database_runtime().set_tenant(db, tenant_id)
    assert db.get_bind().dialect.name == "postgresql"
    assert db.scalar(text("SELECT current_user")) == "app_user"
    assert db.scalar(text("SELECT current_setting('app.current_tenant')")) == str(tenant_id)


def _historical_session(db, tenant_id, party_id, *, expired=False, revoked_at=None):
    """Persist setup facts; no fixture setter is used as a race contender."""
    now = datetime.now(UTC)
    row = AuthSession(
        tenant_id=tenant_id,
        party_id=party_id,
        token_hash=hash_token(uuid4().hex),
        expires_at=now + timedelta(hours=-1 if expired else 1),
        revoked_at=revoked_at,
        created_at=now - timedelta(days=3),
        updated_at=revoked_at if revoked_at is not None else now,
    )
    db.add(row)
    db.flush()
    return row


@pytest.fixture
def status_sign_in_case(sign_in_account, admin_session, tenant_b, app_client):
    """Actual migrated account, runtime registration and independent app_user roots."""
    assert app_client is not None  # Imports the real composed runtime registration.
    probe, tenant_id, party_id, email, _ = sign_in_account
    _scope(probe, tenant_id)
    existing = credential_authentication.sign_in(
        probe, tenant_id=tenant_id, email=email, password="chosen-password9",
    )
    assert existing is not None
    probe.commit()  # Release initial real sign-in's Party lock before setup/race.

    setup = admin_session
    prior = datetime.now(UTC) - timedelta(days=1)
    ended = _historical_session(setup, tenant_id, party_id, expired=True, revoked_at=prior)
    expired = _historical_session(setup, tenant_id, party_id, expired=True)
    unrelated = []
    for scope in (tenant_id, tenant_b.id):
        outsider = Party(
            tenant_id=scope, party_type=PartyType.person, display_name="Unrelated race account",
            email=f"unrelated-status-login-{uuid4().hex}@example.invalid", is_active=True,
        )
        setup.add(outsider)
        setup.flush()
        setup.add(PartyPerson(party_id=outsider.id, first_name="Unrelated", last_name="Account"))
        setup.flush()
        now = datetime.now(UTC)
        setup.add(AcademyAccountProfile(
            tenant_id=scope, party_id=outsider.id, status="active", prefs={}, created_at=now, updated_at=now,
        ))
        other = _historical_session(setup, scope, outsider.id)
        unrelated.append((scope, outsider.id, other.id, other.updated_at))
    ended_id, expired_id, prior_updated = ended.id, expired.id, ended.updated_at
    setup.commit()
    _scope(probe, tenant_id)
    baseline = tuple(probe.scalars(
        select(AuthSession.id).where(AuthSession.tenant_id == tenant_id, AuthSession.party_id == party_id),
    ))
    assert set(baseline) == {existing.session_id, ended_id, expired_id}
    return SimpleNamespace(
        probe=probe, admin=setup, tenant_id=tenant_id, party_id=party_id, email=email,
        existing=existing, ended_id=ended_id, expired_id=expired_id, prior=prior,
        prior_updated=prior_updated, baseline=baseline, unrelated=tuple(unrelated),
    )


def _poll_blocked(probe, statement, params):
    # Backend activity is cached for the rest of a transaction: drop the cached
    # snapshot so each poll sees the follower's current wait state.
    probe.execute(text("SELECT pg_stat_clear_snapshot()"))
    return probe.scalar(statement, params)


def _wait_for_actual_leader_lock(probe, follower_pid, leader_pid):
    deadline = monotonic() + 10
    while not _poll_blocked(
        probe,
        text(
            "SELECT a.wait_event_type = 'Lock' "
            "AND :leader_pid = ANY(pg_blocking_pids(a.pid)) "
            "AND position('parties' in a.query) > 0 "
            "AND position('FOR UPDATE' in a.query) > 0 "
            "FROM pg_stat_activity a WHERE a.pid = :follower_pid"
        ),
        {"leader_pid": leader_pid, "follower_pid": follower_pid},
    ):
        assert monotonic() < deadline, "contender never blocked on the actual service leader's row lock"
        sleep(0.01)


@pytest.mark.parametrize("first", ["login", "suspension"])
def test_actual_target_suspension_and_sign_in_serialize_both_orders(status_sign_in_case, first):
    c = status_sign_in_case
    factory = sessionmaker(bind=c.probe.get_bind(), autoflush=False)
    leader_done, follower_started, release_leader = Event(), Event(), Event()
    backend_pids, results, completed_roots = {}, {}, {}

    def run(kind, leader):
        with factory() as db:
            _scope(db, c.tenant_id)
            root = db.get_transaction()
            assert root is not None and root.is_active
            backend_pids[kind] = db.scalar(text("SELECT pg_backend_pid()"))
            # Retain genuinely stale aliases: the first service's changes are
            # uncommitted when the follower reads these before its Party wait.
            cached = (
                db.get(Party, c.party_id),
                db.get(AcademyAccountProfile, c.party_id),
                db.get(AuthSession, c.existing.session_id),
            )
            assert cached[0].is_active and cached[1].status == "active"
            assert cached[2].revoked_at is None
            if not leader:
                assert leader_done.wait(10)
                follower_started.set()
            if kind == "login":
                results[kind] = credential_authentication.sign_in(
                    db, tenant_id=c.tenant_id, email=c.email, password="chosen-password9",
                )
            else:
                profile = canonical_lifecycle.set_account_status(
                    db, tenant_id=c.tenant_id, person_id=c.party_id, status="suspended",
                )
                assert profile is cached[1] and profile.status == "suspended"
                results[kind] = profile.updated_at
            # The real public savepoint has closed, but the caller root retains
            # its Party lock and tenant GUC until this test boundary commits.
            assert db.get_transaction() is root and db.is_active
            assert db.get_nested_transaction() is None
            assert db.scalar(text("SELECT current_setting('app.current_tenant')")) == str(c.tenant_id)
            if leader:
                leader_done.set()  # Actual service completed; no manual winner DML.
                assert release_leader.wait(10)
            db.commit()
            assert db.get_transaction() is None
            completed_roots[kind] = True

    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = pool.submit(run, first, True)
        follower_kind = "suspension" if first == "login" else "login"
        follower = None
        try:
            assert leader_done.wait(10)
            follower = pool.submit(run, follower_kind, False)
            assert follower_started.wait(10)
            _wait_for_actual_leader_lock(c.probe, backend_pids[follower_kind], backend_pids[first])
        finally:
            release_leader.set()
        leader.result(timeout=20)
        assert follower is not None
        follower.result(timeout=20)
    assert completed_roots == {"login": True, "suspension": True}

    c.probe.rollback()  # Fresh independent read transaction, no cached proof.
    _scope(c.probe, c.tenant_id)
    c.probe.expire_all()
    profile = c.probe.get(AcademyAccountProfile, c.party_id)
    assert profile.status == "suspended" and profile.updated_at == results["suspension"]
    assert c.probe.get(Party, c.party_id).is_active is True
    persisted = tuple(c.probe.scalars(select(AuthSession).where(
        AuthSession.tenant_id == c.tenant_id, AuthSession.party_id == c.party_id,
    )))
    assert all(row.revoked_at is not None for row in persisted)
    prior = c.probe.get(AuthSession, c.ended_id)
    assert prior.revoked_at == c.prior and prior.updated_at == c.prior_updated
    assert c.probe.get(AuthSession, c.expired_id).revoked_at == profile.updated_at
    assert c.probe.get(AuthSession, c.existing.session_id).revoked_at == profile.updated_at
    assert web_auth._current_person(c.probe, c.tenant_id, c.existing.session_token) is None
    login = results["login"]
    if first == "login":
        assert login is not None and login.session_id not in c.baseline
        assert len(persisted) == len(c.baseline) + 1
        captured = c.probe.get(AuthSession, login.session_id)
        assert captured.revoked_at == captured.updated_at == profile.updated_at
        assert web_auth._current_person(c.probe, c.tenant_id, login.session_token) is None
    else:
        assert login is None and {row.id for row in persisted} == set(c.baseline)

    # The same-tenant outsider remains visible and unaffected through app_user;
    # RLS hides the other tenant, whose unchanged facts the setup owner observes.
    for scope, outsider_id, session_id, updated_at in c.unrelated:
        if scope == c.tenant_id:
            row = c.probe.get(AuthSession, session_id)
            assert row.revoked_at is None and row.updated_at == updated_at
            assert c.probe.get(AcademyAccountProfile, outsider_id).status == "active"
        else:
            assert c.probe.get(AuthSession, session_id) is None
        c.admin.expire_all()
        actual = c.admin.get(AuthSession, session_id)
        assert actual.revoked_at is None and actual.updated_at == updated_at
        assert c.admin.get(AcademyAccountProfile, outsider_id).status == "active"
        assert c.admin.get(Party, outsider_id).is_active is True
