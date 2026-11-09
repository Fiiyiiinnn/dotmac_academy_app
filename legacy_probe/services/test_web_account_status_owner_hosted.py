"""Authored PostgreSQL/app_user/RLS/race canaries. DO NOT run locally.

Uses migrated canonical fixtures and actual installed Kernel storage. Direct
route calls carry real Request cookies; these are not full HTTP/client tests.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from time import monotonic, sleep
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from dotmac_kernel.identity_storage import lock_grants, lock_parties, remove_grants
from dotmac_kernel.models import (
    AuthSession,
    ExternalIdentityBinding,
    Party,
    PartyPerson,
    PartyRoleGrant,
    PartyType,
    Role,
)
from dotmac_kernel.session_runtime import resolve_database_runtime
from dotmac_kernel.transactions import conflict_savepoint
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from app.models.identity_extensions import AcademyAccountProfile
from app.services import canonical_lifecycle as lifecycle
from app.services import web_auth
from app.services.security import hash_token
from app.web import accounts, instructor
from legacy_probe.services.test_canonical_current_user import current_account

assert current_account


def _request(tenant, token):
    headers = [] if token is None else [(b"cookie", f"session={token}".encode())]
    return Request({"type": "http", "headers": headers, "state": {"tenant": tenant}})


def _scope(db, tid):
    resolve_database_runtime().set_tenant(db, tid)
    assert db.scalar(text("SELECT current_user")) == "app_user"


def _wait_for_lock(db, pid):
    deadline = monotonic() + 10
    while not db.scalar(text("SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid=:pid"), {"pid": pid}):
        assert monotonic() < deadline, "contender never waited on an actual PostgreSQL row lock"
        sleep(0.01)


def _session(db, tid, pid, *, expired=False, binding=None, revoked=None):
    row = AuthSession(
        tenant_id=tid, party_id=pid, token_hash=hash_token(uuid4().hex),
        expires_at=datetime.now(UTC) + timedelta(hours=-1 if expired else 1),
        external_identity_binding_id=binding, revoked_at=revoked,
    )
    db.add(row)
    db.flush()
    return row


@pytest.fixture(params=["actor_first", "target_first"])
def admin_write_case(current_account, request):
    a = current_account
    setup = a.admin
    now = datetime.now(UTC)
    # Force both principal orderings; no test depends on random UUID ordering.
    delta = 1 if request.param == "actor_first" else -1
    target_id = UUID(int=a.pid.int + delta)
    roles = {}
    for slug in ("admin", "instructor", "student", "custom-account-owner-test"):
        role = setup.scalar(select(Role).where(Role.tenant_id == a.tid, Role.slug == slug))
        if role is None:
            role = Role(tenant_id=a.tid, slug=slug, name=slug)
            setup.add(role)
            setup.flush()
        roles[slug] = role.id
    if setup.scalar(select(PartyRoleGrant.id).where(
        PartyRoleGrant.tenant_id == a.tid, PartyRoleGrant.party_id == a.pid,
        PartyRoleGrant.role_id == roles["admin"],
    )) is None:
        setup.add(PartyRoleGrant(tenant_id=a.tid, party_id=a.pid, role_id=roles["admin"]))
    setup.add(Party(
        id=target_id, tenant_id=a.tid, party_type=PartyType.person, display_name="Status Target",
        email=f"status-target-{uuid4().hex}@a.edu", is_active=True,
    ))
    setup.flush()
    setup.add(PartyPerson(party_id=target_id, first_name="Status", last_name="Target"))
    setup.flush()
    setup.add(AcademyAccountProfile(
        tenant_id=a.tid, party_id=target_id, status="active", prefs={}, created_at=now, updated_at=now,
    ))
    setup.add_all([
        PartyRoleGrant(tenant_id=a.tid, party_id=target_id, role_id=roles["student"]),
        PartyRoleGrant(tenant_id=a.tid, party_id=target_id, role_id=roles["custom-account-owner-test"]),
    ])
    setup.flush()
    ordinary = _session(setup, a.tid, target_id)
    expired = _session(setup, a.tid, target_id, expired=True)
    binding = ExternalIdentityBinding(
        tenant_id=a.tid, party_id=target_id, provider_binding="fixture-provider", issuer="https://fixture.invalid",
        subject=uuid4().hex, is_active=True, bound_by="hosted-test-fixture", bind_reason="fixture provenance",
    )
    setup.add(binding)
    setup.flush()
    external = _session(setup, a.tid, target_id, expired=True, binding=binding.id)
    prior = datetime.now(UTC) - timedelta(days=1)
    ended = _session(setup, a.tid, target_id, revoked=prior)
    setup.commit()
    a.db.rollback()
    _scope(a.db, a.tid)
    a.db.expire_all()
    return SimpleNamespace(
        actor=a, tid=a.tid, target=target_id, roles=roles,
        selected=(ordinary.id, expired.id, external.id), ended=ended.id, prior=prior,
    )


def _action(db, case, route):
    a = case.actor
    req = _request(a.tenant, a.token)
    actor = web_auth.require_web_user(req, db)
    if route == "accounts":
        return accounts.users_status(case.target, req, status_value="suspended", actor=actor, db=db)
    if route == "instructor":
        return instructor.change_account_status(case.target, req, status_value="suspended", db=db)
    return accounts.users_role(case.target, req, role="instructor", actor=actor, db=db)


def test_internal_owner_exact_expired_external_membership_refresh_and_root(admin_write_case, monkeypatch):
    c = admin_write_case
    a, db = c.actor, c.actor.db
    loaded = [db.get(AuthSession, sid) for sid in (*c.selected, c.ended)]
    actor_session = db.get(AuthSession, a.session_id)
    root = db.get_transaction()
    seen = []
    owner = lifecycle.revoke_party_sessions

    def observed(actual, **values):
        assert actual is db and values["tenant_id"] == c.tid and values["party_id"] == c.target
        assert values["session_ids"] == tuple(sorted(c.selected))
        seen.append(values["session_ids"])
        return owner(actual, **values)

    monkeypatch.setattr(lifecycle, "revoke_party_sessions", observed)
    profile = lifecycle.set_account_status(db, tenant_id=c.tid, person_id=c.target, status="suspended")
    assert seen == [tuple(sorted(c.selected))]
    assert profile.status == "suspended" and db.get(Party, c.target).is_active
    assert all(row.revoked_at == row.updated_at == profile.updated_at for row in loaded[:-1])
    assert loaded[-1].revoked_at == c.prior and actor_session.revoked_at is None
    assert db.get_transaction() is root
    assert db.scalar(text("SELECT current_setting('app.current_tenant')")) == str(c.tid)
    stamps = tuple(row.revoked_at for row in loaded)
    lifecycle.set_account_status(db, tenant_id=c.tid, person_id=c.target, status="active")
    assert tuple(row.revoked_at for row in loaded) == stamps and len(seen) == 1


@pytest.mark.parametrize("step", ["core", "profile_flush"])
def test_internal_status_failure_rollback_preserves_earlier_flushed_root_work(admin_write_case, monkeypatch, step):
    c = admin_write_case
    db = c.actor.db
    profile = db.get(AcademyAccountProfile, c.target)
    profile.prefs = {"earlier_flushed_root_work": True}
    db.flush()
    before_stamp = profile.updated_at
    root = db.get_transaction()
    original_owner, original_flush = lifecycle.revoke_party_sessions, db.flush
    injected = False

    def fail_core(*args, **kwargs):
        original_owner(*args, **kwargs)
        raise IntegrityError("injected after actual Core revocation", {}, RuntimeError("fixture"))

    def fail_flush(*args, **kwargs):
        nonlocal injected
        original_flush(*args, **kwargs)
        if profile.status == "suspended" and not injected:
            injected = True
            raise IntegrityError("injected after actual profile flush", {}, RuntimeError("fixture"))

    if step == "core":
        monkeypatch.setattr(lifecycle, "revoke_party_sessions", fail_core)
    else:
        monkeypatch.setattr(db, "flush", fail_flush)
    with pytest.raises(lifecycle.BadRequestError, match="account lifecycle conflict"):
        lifecycle.set_account_status(db, tenant_id=c.tid, person_id=c.target, status="suspended")
    monkeypatch.setattr(db, "flush", original_flush)
    assert db.get_transaction() is root and db.is_active
    assert db.scalar(text("SELECT current_setting('app.current_tenant')")) == str(c.tid)
    db.commit()  # A normal successful outer boundary cannot commit partial effects.
    _scope(db, c.tid)
    db.expire_all()
    assert profile.status == "active" and profile.updated_at == before_stamp
    assert profile.prefs == {"earlier_flushed_root_work": True}
    assert all(db.get(AuthSession, sid).revoked_at is None for sid in c.selected)
    assert db.get(AuthSession, c.ended).revoked_at == c.prior


def _invalidate(db, c, kind):
    a = c.actor
    if kind == "suspended":
        lifecycle.set_account_status(db, tenant_id=c.tid, person_id=a.pid, status="suspended")
    elif kind == "logout":
        web_auth.revoke_session(db, c.tid, a.token)
    elif kind == "reset":
        lifecycle.reset_password(db, tenant_id=c.tid, raw=c.reset_token, new_password="replacement-password9")
    else:
        with conflict_savepoint(db):
            db.connection()
            lock_parties(db, tenant_id=c.tid, party_ids=(a.pid,))
            selected = lock_grants(db, tenant_id=c.tid, party_ids=(a.pid,))
            remove_grants(
                db, tenant_id=c.tid, party_id=a.pid,
                expected=tuple(g for g in selected if g.role_id == c.roles["admin"]),
            )


@pytest.mark.parametrize("route", ["accounts", "instructor", "role"])
@pytest.mark.parametrize("invalidator", ["demoted", "suspended", "logout", "reset"])
@pytest.mark.parametrize("first", ["action", "invalidation"])
def test_real_actor_invalidation_and_route_lock_races_both_orders(admin_write_case, route, invalidator, first):
    c = admin_write_case
    a, probe = c.actor, c.actor.db
    if invalidator == "reset":
        c.reset_token = lifecycle.request_password_reset(probe, tenant_id=c.tid, email=a.email)
        assert c.reset_token is not None
    probe.commit()
    _scope(probe, c.tid)
    factory = sessionmaker(bind=probe.get_bind(), autoflush=False)
    locked, release, started = Event(), Event(), Event()
    follower_pid, outcomes = [], {}

    def work(kind, leader):
        with factory() as db:
            _scope(db, c.tid)
            # Deliberately retain stale aliases/read projection before waiting.
            if kind == "action":
                cached_actor = (
                    db.get(Party, a.pid), db.get(AcademyAccountProfile, a.pid),
                    db.get(AuthSession, a.session_id),
                    tuple(db.scalars(select(PartyRoleGrant).where(PartyRoleGrant.party_id == a.pid))),
                )
                assert all(row is not None for row in cached_actor[:3])
                assert web_auth.require_web_user(_request(a.tenant, a.token), db).id == a.pid
            if leader:
                if kind == "invalidation":
                    lock_parties(db, tenant_id=c.tid, party_ids=(a.pid,))
                    locked.set()
                    assert release.wait(10)
                else:
                    outcomes["response"] = _action(db, c, route)
                    locked.set()  # Action still holds its Party locks until commit.
                    assert release.wait(10)
            else:
                assert locked.wait(10)
                follower_pid.append(db.scalar(text("SELECT pg_backend_pid()")))
                started.set()
            if kind == "invalidation":
                _invalidate(db, c, invalidator)
            elif not leader:
                with pytest.raises(HTTPException) as refused:
                    _action(db, c, route)
                outcomes["refusal"] = refused.value.status_code
            db.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        leader_kind = "action" if first == "action" else "invalidation"
        leader = pool.submit(work, leader_kind, True)
        follower = None
        try:
            assert locked.wait(10)
            follower = pool.submit(work, "invalidation" if first == "action" else "action", False)
            assert started.wait(10)
            _wait_for_lock(probe, follower_pid[0])
        finally:
            release.set()
        leader.result(timeout=20)
        assert follower is not None
        follower.result(timeout=20)
    probe.expire_all()
    if first == "invalidation":
        assert outcomes["refusal"] == (403 if invalidator == "demoted" else 303)
        assert probe.get(AcademyAccountProfile, c.target).status == "active"
        assert all(probe.get(AuthSession, sid).revoked_at is None for sid in c.selected)
        actual = set(probe.scalars(select(PartyRoleGrant.role_id).where(PartyRoleGrant.party_id == c.target)))
        assert c.roles["student"] in actual and c.roles["instructor"] not in actual
    else:
        assert outcomes["response"].status_code in (200, 303)
        if route != "role":
            assert probe.get(AcademyAccountProfile, c.target).status == "suspended"
        else:
            actual = set(probe.scalars(select(PartyRoleGrant.role_id).where(PartyRoleGrant.party_id == c.target)))
            assert c.roles["instructor"] in actual and c.roles["custom-account-owner-test"] in actual
            assert c.roles["student"] not in actual


@pytest.mark.parametrize("route", ["accounts", "instructor", "role"])
def test_final_target_lock_wait_crossing_cookie_expiry_refuses(admin_write_case, route):
    c = admin_write_case
    a, probe = c.actor, c.actor.db
    session = probe.get(AuthSession, a.session_id)
    session.expires_at = datetime.now(UTC) + timedelta(seconds=2)
    expires = session.expires_at
    probe.commit()
    _scope(probe, c.tid)
    factory = sessionmaker(bind=probe.get_bind(), autoflush=False)
    started, pids = Event(), []

    def action():
        with factory() as db:
            _scope(db, c.tid)
            pids.append(db.scalar(text("SELECT pg_backend_pid()")))
            started.set()
            with pytest.raises(HTTPException) as refused:
                _action(db, c, route)
            assert refused.value.status_code == 303
            db.commit()

    with factory() as blocker, ThreadPoolExecutor(max_workers=1) as pool:
        _scope(blocker, c.tid)
        if route == "role":
            blocker.scalar(select(PartyRoleGrant).where(
                PartyRoleGrant.party_id == c.target, PartyRoleGrant.role_id == c.roles["student"],
            ).with_for_update())
        else:
            blocker.scalar(select(AuthSession).where(AuthSession.id == c.selected[0]).with_for_update())
        future = pool.submit(action)
        try:
            assert started.wait(10)
            _wait_for_lock(probe, pids[0])
            deadline = monotonic() + 10
            while datetime.now(UTC) <= expires:
                assert monotonic() < deadline
                sleep(0.01)
        finally:
            blocker.commit()
        future.result(timeout=20)
    probe.expire_all()
    assert probe.get(AcademyAccountProfile, c.target).status == "active"
    assert all(probe.get(AuthSession, sid).revoked_at is None for sid in c.selected)


@pytest.mark.parametrize("cookie", [
    "missing", "tampered", "revoked", "expired", "wrong_tenant", "inactive", "suspended",
    "profile_deleted", "subtype_deleted", "blank_email",
])
def test_cookie_refusals_preserve_target_and_refresh_stale_identity(admin_write_case, tenant_b, cookie):
    c = admin_write_case
    a, db = c.actor, c.actor.db
    cached = (db.get(Party, a.pid), db.get(AcademyAccountProfile, a.pid), db.get(AuthSession, a.session_id))
    if cookie in {"revoked", "expired"}:
        row = a.admin.get(AuthSession, a.session_id)
        if cookie == "revoked":
            row.revoked_at = datetime.now(UTC)
        else:
            row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        a.admin.commit()
        assert cached[2].revoked_at is None
    if cookie in {"inactive", "suspended", "profile_deleted", "subtype_deleted", "blank_email"}:
        if cookie == "inactive":
            a.admin.get(Party, a.pid).is_active = False
        elif cookie == "suspended":
            a.admin.get(AcademyAccountProfile, a.pid).status = "suspended"
        elif cookie == "blank_email":
            a.admin.get(Party, a.pid).email = ""
        else:
            model = AcademyAccountProfile if cookie == "profile_deleted" else PartyPerson
            a.admin.delete(a.admin.get(model, a.pid))
        a.admin.commit()
    token = None if cookie == "missing" else a.token + "x" if cookie == "tampered" else a.token
    req = _request(tenant_b if cookie == "wrong_tenant" else a.tenant, token)
    with pytest.raises(HTTPException) as refused:
        lifecycle.set_web_account_status(db, req, person_id=c.target, status="suspended", forbidden_detail="Forbidden")
    assert refused.value.status_code == 303
    assert db.get(AcademyAccountProfile, c.target).status == "active"
    assert all(db.get(AuthSession, sid).revoked_at is None for sid in c.selected)


def test_self_suspension_remains_allowed_and_revokes_actual_cookie(admin_write_case):
    c = admin_write_case
    a, db = c.actor, c.actor.db
    lifecycle.set_web_account_status(
        db, _request(a.tenant, a.token), person_id=a.pid, status="suspended", forbidden_detail="Forbidden",
    )
    assert db.get(AcademyAccountProfile, a.pid).status == "suspended"
    assert db.get(AuthSession, a.session_id).revoked_at is not None


def test_empty_revocation_cohort_never_widens_or_rewrites_prior_stamps(admin_write_case, monkeypatch):
    c = admin_write_case
    db = c.actor.db
    first = datetime.now(UTC) - timedelta(minutes=1)
    lifecycle.set_account_status(db, tenant_id=c.tid, person_id=c.target, status="suspended", now=first)
    lifecycle.set_account_status(db, tenant_id=c.tid, person_id=c.target, status="active")
    owner = lifecycle.revoke_party_sessions
    observed = []

    def empty(actual, **values):
        assert values["session_ids"] == ()
        observed.append(values["session_ids"])
        return owner(actual, **values)

    monkeypatch.setattr(lifecycle, "revoke_party_sessions", empty)
    lifecycle.set_account_status(db, tenant_id=c.tid, person_id=c.target, status="suspended")
    assert observed == [()]
    assert all(db.get(AuthSession, sid).revoked_at == first for sid in c.selected)
    assert db.get(AuthSession, c.ended).revoked_at == c.prior
    assert db.get(AuthSession, c.actor.session_id).revoked_at is None


def test_revocation_excludes_actual_other_tenant_account(admin_write_case, tenant_b):
    c = admin_write_case
    setup = c.actor.admin
    outsider = Party(
        tenant_id=tenant_b.id, party_type=PartyType.person, display_name="Other tenant",
        email=f"other-status-{uuid4().hex}@b.edu", is_active=True,
    )
    setup.add(outsider)
    setup.flush()
    other = _session(setup, tenant_b.id, outsider.id, expired=True)
    other_id = other.id
    setup.commit()
    lifecycle.set_account_status(c.actor.db, tenant_id=c.tid, person_id=c.target, status="suspended")
    c.actor.db.commit()
    setup.expire_all()
    assert setup.get(AuthSession, other_id).revoked_at is None
