"""Natural hosted PG lock/claim races; authored NOT RUN, no injected clock.

Requires complete canonical fixture/action/owner/FK qualification. Role mutation
below is a test Party-first protocol canary, not conversion of an unowned role
writer. Real authorities always come from persisted cookies/Parties/grants.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from time import monotonic, sleep
from types import SimpleNamespace
from uuid import uuid4

import pytest
from dotmac_kernel.models import AuthSession, Party, PartyRoleGrant, Role, UserCredential
from dotmac_kernel.session_runtime import resolve_database_runtime
from fastapi import HTTPException
from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from app.models.account_token import AccountToken
from app.services import canonical_lifecycle
from app.services import invitation_issuance as issuance
from app.services.exceptions import BadRequestError
from app.services.invitation_claim import accept_invite
from app.services.invitation_policy import invitation_acceptance_policy
from app.services.security import hash_password, hash_token
from app.services.web_auth import revoke_session
from legacy_probe.services.test_canonical_current_user import _request
from legacy_probe.services.test_canonical_invitation_issuance import _credential, _person, _run, issuer
from legacy_probe.services.test_canonical_self_profile_locking import _bind, _wait_for_lock

assert issuer


def _invite(session, a, email):
    with issuance.invite_web_account(
        session, request=_request(a.tenant, a.token), email=email, first_name="New", last_name="Target", role="student"
    ) as result:
        return result


def test_later_principal_wait_already_holds_all_earlier_existing_party_locks(issuer):
    a = issuer
    target = _person(a.db, tenant_id=a.tid, email="later@a.edu", first_name="Later", last_name="Target")
    first_id, last_id = sorted([a.pid, target.id])
    a.db.commit()
    SessionLocal = sessionmaker(bind=a.db.get_bind(), autoflush=False)
    locked, started, release = Event(), Event(), Event()
    backend = []

    def holder():
        with SessionLocal() as db:
            _bind(db, a.tid)
            db.scalars(select(Party).where(Party.tenant_id == a.tid, Party.id == last_id).with_for_update()).one()
            locked.set()
            assert release.wait(10)
            db.commit()

    def writer():
        with SessionLocal() as db:
            _bind(db, a.tid)
            backend.append(db.scalar(text("SELECT pg_backend_pid()")))
            started.set()
            assert _invite(db, a, target.email).person.id == target.id
            db.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = pool.submit(holder)
        assert locked.wait(10)
        follower = pool.submit(writer)
        assert started.wait(10)
        try:
            resolve_database_runtime().set_tenant(a.db, a.tid)
            _wait_for_lock(a.db, backend[0])
            with pytest.raises(DBAPIError) as caught:
                with a.db.begin_nested():
                    a.db.scalars(
                        select(Party).where(Party.tenant_id == a.tid, Party.id == first_id).with_for_update(nowait=True)
                    ).one()
            assert getattr(caught.value.orig, "sqlstate", None) == "55P03"
        finally:
            release.set()
        leader.result(timeout=20)
        follower.result(timeout=20)


def test_reciprocal_actor_target_invitations_share_uuid_order_without_deadlock(issuer):
    a = issuer
    target = _person(a.db, tenant_id=a.tid, email="reciprocal@a.edu", first_name="Other", last_name="Admin")
    _credential(
        a.db,
        tenant_id=a.tid,
        person_id=target.id,
        email=target.email,
        password_hash=hash_password("genuine-fixture-password9"),
    )
    role = a.db.scalars(select(Role).where(Role.tenant_id == a.tid, Role.slug == "admin")).one()
    a.db.add(PartyRoleGrant(tenant_id=a.tid, party_id=target.id, role_id=role.id))
    token = uuid4().hex + uuid4().hex
    a.db.add(
        AuthSession(
            tenant_id=a.tid,
            party_id=target.id,
            token_hash=hash_token(token),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    a.db.commit()
    other = SimpleNamespace(tenant=a.tenant, token=token)
    SessionLocal = sessionmaker(bind=a.db.get_bind(), autoflush=False)
    locked, started, release = Event(), Event(), Event()
    backend = []
    minimum = min(a.pid, target.id)

    def writer(actor, email, leader):
        with SessionLocal() as db:
            _bind(db, a.tid)
            if leader:
                db.scalars(select(Party).where(Party.tenant_id == a.tid, Party.id == minimum).with_for_update()).one()
                locked.set()
                assert release.wait(10)
            else:
                backend.append(db.scalar(text("SELECT pg_backend_pid()")))
                started.set()
            result = _invite(db, actor, email)
            assert result.had_credential and result.token is None
            db.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = pool.submit(writer, a, target.email, True)
        assert locked.wait(10)
        follower = pool.submit(writer, other, a.email, False)
        assert started.wait(10)
        try:
            resolve_database_runtime().set_tenant(a.db, a.tid)
            _wait_for_lock(a.db, backend[0])
        finally:
            release.set()
        leader.result(timeout=20)
        follower.result(timeout=20)


@pytest.mark.parametrize("restriction", ["status", "revocation", "role", "expiry"])
@pytest.mark.parametrize("target_kind", ["new_account", "missing_resend"])
def test_postwait_live_actor_cookie_status_role_and_actual_clock_are_revalidated(issuer, restriction, target_kind):
    a = issuer
    expiry = datetime.now(UTC) + timedelta(seconds=3)
    if restriction == "expiry":
        a.admin.get(AuthSession, a.session_id).expires_at = expiry
        a.admin.commit()
    a.db.commit()
    SessionLocal = sessionmaker(bind=a.db.get_bind(), autoflush=False)
    locked, started, release = Event(), Event(), Event()
    backend = []

    def holder():
        with SessionLocal() as db:
            _bind(db, a.tid)
            db.scalars(select(Party).where(Party.tenant_id == a.tid, Party.id == a.pid).with_for_update()).one()
            locked.set()
            assert release.wait(10)
            if restriction == "status":
                canonical_lifecycle.set_account_status(db, tenant_id=a.tid, person_id=a.pid, status="suspended")
            elif restriction == "revocation":
                revoke_session(db, a.tid, a.token)
            elif restriction == "role":
                db.execute(
                    delete(PartyRoleGrant).where(PartyRoleGrant.tenant_id == a.tid, PartyRoleGrant.party_id == a.pid)
                )
            db.commit()

    def writer():
        with SessionLocal() as db:
            _bind(db, a.tid)
            backend.append(db.scalar(text("SELECT pg_backend_pid()")))
            started.set()
            with pytest.raises(HTTPException) as caught:
                if target_kind == "missing_resend":
                    with issuance.resend_web_account_invitation(
                        db, request=_request(a.tenant, a.token), person_id=uuid4()
                    ):
                        pytest.fail("unauthorized missing target must never reach delivery")
                else:
                    _invite(db, a, "refused-after-wait@a.edu")
            assert caught.value.status_code == (403 if restriction == "role" else 303)
            db.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = pool.submit(holder)
        assert locked.wait(10)
        follower = pool.submit(writer)
        assert started.wait(10)
        try:
            resolve_database_runtime().set_tenant(a.db, a.tid)
            _wait_for_lock(a.db, backend[0])
            if restriction == "expiry":
                deadline = monotonic() + 10
                while datetime.now(UTC) <= expiry:
                    assert monotonic() < deadline
                    sleep(0.01)
        finally:
            release.set()
        leader.result(timeout=20)
        follower.result(timeout=20)
    assert (
        a.db.scalars(select(Party).where(Party.tenant_id == a.tid, Party.email == "refused-after-wait@a.edu")).first()
        is None
    )


@pytest.mark.parametrize("first", ["claim", "resend"])
def test_actual_claim_and_human_resend_refresh_complete_membership_after_wait(issuer, first):
    a = issuer
    original = _run(a, email="race-claim@a.edu", first_name="Race", last_name="Claim", role="student")
    a.db.commit()
    SessionLocal = sessionmaker(bind=a.db.get_bind(), autoflush=False)
    locked, started, release = Event(), Event(), Event()
    backend = []
    outcomes = {}

    def work(kind, leader):
        with SessionLocal() as db:
            _bind(db, a.tid)
            if leader:
                db.scalars(
                    select(Party).where(Party.tenant_id == a.tid, Party.id == original.person.id).with_for_update()
                ).one()
                locked.set()
                assert release.wait(10)
            else:
                backend.append(db.scalar(text("SELECT pg_backend_pid()")))
                started.set()
            if kind == "claim":
                if first == "resend":
                    with pytest.raises(BadRequestError, match="invalid or expired token"):
                        accept_invite(
                            db,
                            tenant_id=a.tid,
                            raw=original.token,
                            password="claim-race9",
                            password_policy=invitation_acceptance_policy(db),
                        )
                    outcomes[kind] = "refused"
                else:
                    outcomes[kind] = accept_invite(
                        db,
                        tenant_id=a.tid,
                        raw=original.token,
                        password="claim-race9",
                        password_policy=invitation_acceptance_policy(db),
                    ).id
            else:
                with issuance.resend_web_account_invitation(
                    db, request=_request(a.tenant, a.token), person_id=original.person.id
                ) as result:
                    outcomes[kind] = (result.had_credential, result.token is not None)
            db.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = pool.submit(work, first, True)
        assert locked.wait(10)
        follower = pool.submit(work, "resend" if first == "claim" else "claim", False)
        assert started.wait(10)
        try:
            resolve_database_runtime().set_tenant(a.db, a.tid)
            _wait_for_lock(a.db, backend[0])
        finally:
            release.set()
        leader.result(timeout=20)
        follower.result(timeout=20)
    a.db.expire_all()
    if first == "claim":
        assert outcomes["claim"] == original.person.id and outcomes["resend"] == (True, False)
        assert (
            a.db.scalars(select(UserCredential).where(UserCredential.party_id == original.person.id)).first()
            is not None
        )
    else:
        assert outcomes["claim"] == "refused" and outcomes["resend"] == (False, True)
        assert a.db.scalars(select(UserCredential).where(UserCredential.party_id == original.person.id)).first() is None
    tokens = a.db.scalars(
        select(AccountToken).where(AccountToken.person_id == original.person.id, AccountToken.kind == "invite")
    ).all()
    assert sum(row.used_at is None for row in tokens) == (0 if first == "claim" else 1)
