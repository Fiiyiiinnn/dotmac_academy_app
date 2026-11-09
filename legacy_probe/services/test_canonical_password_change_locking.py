"""Hosted real row-lock races; actual clock sampled by the product adapter.

No injected clock provides a concurrency claim. Hosted fixture/action/FK
qualification remains pending; these tests are authored, not locally run.
"""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from dotmac_kernel.models import Party, PartyPerson
from dotmac_kernel.session_runtime import resolve_database_runtime
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from app.services import canonical_lifecycle
from app.services.web_auth import revoke_session
from legacy_probe.services.test_canonical_current_user import current_account
from legacy_probe.services.test_canonical_password_change import _change
from legacy_probe.services.test_canonical_self_profile_locking import _bind, _wait_for_lock

assert current_account


@pytest.mark.parametrize("leader_operation", ["status", "revocation", "password_change"])
def test_waiting_password_change_refuses_after_actual_principal_writer_commits(current_account, leader_operation):
    a = current_account
    a.db.commit()
    SessionLocal = sessionmaker(bind=a.db.get_bind(), autoflush=False)
    locked, started, release = Event(), Event(), Event()
    backend = []

    def leader():
        with SessionLocal() as session:
            _bind(session, a.tid)
            session.scalars(select(Party).where(Party.tenant_id == a.tid, Party.id == a.pid).with_for_update()).one()
            locked.set()
            assert release.wait(10)
            if leader_operation == "status":
                canonical_lifecycle.set_account_status(session, tenant_id=a.tid, person_id=a.pid, status="suspended")
            elif leader_operation == "revocation":
                revoke_session(session, a.tid, a.token)
            else:
                from types import SimpleNamespace

                assert _change(SimpleNamespace(db=session, tenant=a.tenant, token=a.token)).ok
            session.commit()

    def follower():
        from types import SimpleNamespace

        with SessionLocal() as session:
            _bind(session, a.tid)
            backend.append(session.scalar(text("SELECT pg_backend_pid()")))
            started.set()
            with pytest.raises(HTTPException) as caught:
                _change(SimpleNamespace(db=session, tenant=a.tenant, token=a.token))
            assert caught.value.status_code == 303
            session.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(leader)
        assert locked.wait(10)
        second = pool.submit(follower)
        assert started.wait(10)
        try:
            resolve_database_runtime().set_tenant(a.db, a.tid)
            _wait_for_lock(a.db, backend[0])
        finally:
            release.set()
        first.result(timeout=20)
        second.result(timeout=20)
    a.db.expire_all()
    assert a.db.get(PartyPerson, a.pid).first_name == "Sign"


def test_natural_subtype_wait_proves_password_writer_acquires_party_first(current_account):
    a = current_account
    a.db.commit()
    SessionLocal = sessionmaker(bind=a.db.get_bind(), autoflush=False)
    locked, started, release = Event(), Event(), Event()
    backend = []

    def holder():
        with SessionLocal() as session:
            _bind(session, a.tid)
            session.scalars(select(PartyPerson).where(PartyPerson.party_id == a.pid).with_for_update()).one()
            locked.set()
            assert release.wait(10)
            session.commit()

    def writer():
        from types import SimpleNamespace

        with SessionLocal() as session:
            _bind(session, a.tid)
            backend.append(session.scalar(text("SELECT pg_backend_pid()")))
            started.set()
            assert _change(SimpleNamespace(db=session, tenant=a.tenant, token=a.token)).ok
            session.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(holder)
        assert locked.wait(10)
        second = pool.submit(writer)
        assert started.wait(10)
        try:
            resolve_database_runtime().set_tenant(a.db, a.tid)
            _wait_for_lock(a.db, backend[0])
            with pytest.raises(DBAPIError) as caught:
                with a.db.begin_nested():
                    a.db.scalars(
                        select(Party).where(Party.tenant_id == a.tid, Party.id == a.pid).with_for_update(nowait=True)
                    ).one()
            assert getattr(caught.value.orig, "sqlstate", None) == "55P03"
        finally:
            release.set()
        first.result(timeout=20)
        second.result(timeout=20)


def test_cookie_expiry_during_real_party_lock_wait_uses_postwait_actual_clock(current_account):
    from datetime import UTC, datetime, timedelta
    from time import monotonic, sleep
    from types import SimpleNamespace

    from dotmac_kernel.models import AuthSession

    a = current_account
    expiry = datetime.now(UTC) + timedelta(seconds=3)
    a.admin.get(AuthSession, a.session_id).expires_at = expiry
    a.admin.commit()
    a.db.commit()
    SessionLocal = sessionmaker(bind=a.db.get_bind(), autoflush=False)
    locked, started, release = Event(), Event(), Event()
    backend = []

    def holder():
        with SessionLocal() as session:
            _bind(session, a.tid)
            session.scalars(select(Party).where(Party.tenant_id == a.tid, Party.id == a.pid).with_for_update()).one()
            locked.set()
            assert release.wait(10)
            session.commit()

    def writer():
        with SessionLocal() as session:
            _bind(session, a.tid)
            backend.append(session.scalar(text("SELECT pg_backend_pid()")))
            started.set()
            with pytest.raises(HTTPException) as caught:
                _change(SimpleNamespace(db=session, tenant=a.tenant, token=a.token))
            assert caught.value.status_code == 303
            session.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(holder)
        assert locked.wait(10)
        second = pool.submit(writer)
        assert started.wait(10)
        try:
            resolve_database_runtime().set_tenant(a.db, a.tid)
            _wait_for_lock(a.db, backend[0])
            deadline = monotonic() + 10
            while datetime.now(UTC) <= expiry:
                assert monotonic() < deadline
                sleep(0.01)
        finally:
            release.set()
        first.result(timeout=20)
        second.result(timeout=20)
