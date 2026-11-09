"""Hosted real PostgreSQL lock waits; app_user writes, never local execution."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from time import monotonic, sleep

import pytest
from dotmac_kernel.models import AuthSession, Party, PartyPerson
from dotmac_kernel.session_runtime import resolve_database_runtime
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from app.models.identity_extensions import AcademyAccountProfile
from app.services import account_profile as writer
from app.services import canonical_lifecycle
from app.services.web_auth import revoke_session
from legacy_probe.services.test_canonical_current_user import _request, current_account

assert current_account


def _wait_for_lock(observer, pid):
    deadline = monotonic() + 10
    while not observer.scalar(
        text("SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = :pid"), {"pid": pid}
    ):
        assert monotonic() < deadline, "writer never reached the genuine row lock wait"
        sleep(0.01)


def _bind(session, tenant_id):
    assert session.scalar(text("SELECT current_user")) == "app_user"
    resolve_database_runtime().set_tenant(session, tenant_id)


@pytest.mark.parametrize("restriction", ["revocation", "status", "expiry"])
def test_postwait_authority_revalidation_refuses_after_principal_writer(current_account, restriction, monkeypatch):
    a = current_account
    # Release observer transaction before independent concurrent writer roots.
    a.db.commit()
    SessionLocal = sessionmaker(bind=a.db.get_bind(), autoflush=False)
    locked, started, release = Event(), Event(), Event()
    backend = []
    now = datetime.now(UTC)
    if restriction == "expiry":
        session = a.admin.get(AuthSession, a.session_id)
        session.expires_at = now + timedelta(minutes=1)
        a.admin.commit()

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return now + timedelta(minutes=2) if release.is_set() else now

        monkeypatch.setattr(writer, "datetime", Clock)

    def principal_writer():
        with SessionLocal() as session:
            _bind(session, a.tid)
            session.scalars(select(Party).where(Party.tenant_id == a.tid, Party.id == a.pid).with_for_update()).one()
            locked.set()
            assert release.wait(10)
            if restriction == "revocation":
                revoke_session(session, a.tid, a.token)
            elif restriction == "status":
                canonical_lifecycle.set_account_status(session, tenant_id=a.tid, person_id=a.pid, status="suspended")
            session.commit()

    def profile_writer():
        with SessionLocal() as session:
            _bind(session, a.tid)
            backend.append(session.scalar(text("SELECT pg_backend_pid()")))
            started.set()
            with pytest.raises(HTTPException) as caught:
                writer.save_names(
                    session, request=_request(a.tenant, a.token), first_name="Forbidden", last_name="AfterWait"
                )
            assert caught.value.status_code == 303 and caught.value.headers == {"Location": "/login"}
            session.commit()  # No latch or root rollback left by caught refusal.

    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = pool.submit(principal_writer)
        assert locked.wait(10)
        follower = pool.submit(profile_writer)
        assert started.wait(10)
        try:
            resolve_database_runtime().set_tenant(a.db, a.tid)
            _wait_for_lock(a.db, backend[0])
        finally:
            release.set()
        leader.result(timeout=20)
        follower.result(timeout=20)
    a.db.expire_all()
    assert a.db.get(PartyPerson, a.pid).first_name == "Sign"
    assert a.db.get(Party, a.pid).display_name == "Sign In"


def test_natural_subtype_wait_already_holds_party_first(current_account):
    a = current_account
    a.db.commit()
    SessionLocal = sessionmaker(bind=a.db.get_bind(), autoflush=False)
    locked, started, release = Event(), Event(), Event()
    backend = []

    def subtype_holder():
        with SessionLocal() as session:
            _bind(session, a.tid)
            session.scalars(select(PartyPerson).where(PartyPerson.party_id == a.pid).with_for_update()).one()
            locked.set()
            assert release.wait(10)
            session.commit()

    def profile_writer():
        with SessionLocal() as session:
            _bind(session, a.tid)
            backend.append(session.scalar(text("SELECT pg_backend_pid()")))
            started.set()
            result = writer.save_names(
                session, request=_request(a.tenant, a.token), first_name="After", last_name="Subtype"
            )
            assert result.first_name == "After"
            session.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = pool.submit(subtype_holder)
        assert locked.wait(10)
        follower = pool.submit(profile_writer)
        assert started.wait(10)
        try:
            resolve_database_runtime().set_tenant(a.db, a.tid)
            _wait_for_lock(a.db, backend[0])
            # Follower is blocked by subtype, and already owns Party. NOWAIT
            # refusal from a third app_user connection proves natural ordering.
            with pytest.raises(DBAPIError) as caught:
                with a.db.begin_nested():
                    a.db.scalars(
                        select(Party).where(Party.tenant_id == a.tid, Party.id == a.pid).with_for_update(nowait=True)
                    ).one()
            assert getattr(caught.value.orig, "sqlstate", None) == "55P03"
        finally:
            release.set()
        leader.result(timeout=20)
        follower.result(timeout=20)
    a.db.expire_all()
    assert a.db.get(Party, a.pid).display_name == "After Subtype"
    assert a.db.get(AcademyAccountProfile, a.pid).status == "active"
