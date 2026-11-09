# Maintained port of legacy_probe/services/test_ical_v2_owner.py (issue #2 wave 5).
"""Hosted Party-first calendar/lifecycle custody and RFC end-time canaries.

Authored NOT RUN. Uses actual generic locks, canonical lifecycle helper and
calendar rotation; no substituted authority or captured SQL parameters.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from time import monotonic

from dotmac_kernel.session_runtime import resolve_database_runtime
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.services import canonical_identity_store, canonical_lifecycle, ical
from tests.test_issue2_ical_owner import (
    _bind,
    admin_session,
    app_client,
    app_user_session,
    sign_in_account,
    tenant_a,
    tenant_b,
)

assert admin_session and app_client and app_user_session and sign_in_account and tenant_a and tenant_b
from tests.test_issue2_ical_owner import calendar_account, current_account  # noqa: E402

assert calendar_account and current_account


def test_deadline_omits_dtend_and_session_preserves_explicit_end():
    now = datetime(2026, 10, 19, tzinfo=UTC)
    end = now + timedelta(hours=1)
    body = ical.render_feed(
        [
            {"uid": "deadline@example.test", "summary": "Deadline", "starts_at": now},
            {"uid": "session@example.test", "summary": "Session", "starts_at": now, "ends_at": end},
        ],
        calname="Calendar",
        now=now,
    )
    deadline, session = body.split("BEGIN:VEVENT\r\n")[1:]
    assert "DTSTART:20261019T000000Z\r\n" in deadline
    assert "DTEND:" not in deadline
    assert "DTSTART:20261019T000000Z\r\n" in session
    assert "DTEND:20261019T010000Z\r\n" in session
    assert body.count("DTEND:") == 1


def test_rotation_waits_on_party_before_subtype_while_canonical_writer_completes(calendar_account):
    a = calendar_account
    a.db.commit()
    factory = sessionmaker(bind=a.db.get_bind(), autoflush=False)
    ready, started, release = Event(), Event(), Event()
    backend, tokens = {}, []

    def canonical_writer():
        with factory() as db:
            _bind(db, a.tid)
            db.execute(text("SET LOCAL statement_timeout = '15s'"))
            backend["canonical"] = db.scalar(text("SELECT pg_backend_pid()"))
            principal = canonical_identity_store.lock_parties(db, tenant_id=a.tid, party_ids=(a.pid,))
            assert tuple(p.id for p in principal) == (a.pid,)
            ready.set()
            assert release.wait(10)
            # The actual lifecycle owner takes Party, then PartyPerson/profile.
            assert canonical_lifecycle._lock_account(db, tenant_id=a.tid, party_id=a.pid) is not None
            db.commit()

    def rotation():
        with factory() as db:
            _bind(db, a.tid)
            db.execute(text("SET LOCAL statement_timeout = '15s'"))
            backend["rotation"] = db.scalar(text("SELECT pg_backend_pid()"))
            started.set()
            tokens.append(ical.mint_feed_token(db, tenant_id=a.tid, person_id=a.pid))
            db.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(canonical_writer)
        try:
            assert ready.wait(10)
            second = pool.submit(rotation)
            assert started.wait(10)
            resolve_database_runtime().set_tenant(a.db, a.tid)
            deadline = monotonic() + 10
            while True:
                # Observe lock custody only: no statement text, parameters,
                # hashes, token bytes or pg_stat_activity query fields.
                blocked, subtype_touched = a.db.execute(
                    text("""
                    SELECT :canonical = ANY(pg_catalog.pg_blocking_pids(:rotation)),
                           EXISTS (
                               SELECT 1 FROM pg_catalog.pg_locks
                               WHERE pid = :rotation
                                 AND relation = pg_catalog.to_regclass('public.party_persons')
                           )
                """),
                    backend,
                ).one()
                if blocked:
                    assert not subtype_touched, "rotation touched subtype before acquiring principal"
                    break
                assert monotonic() < deadline, "rotation never waited on the canonical principal lock"
        finally:
            release.set()
        first.result(timeout=20)
        second.result(timeout=20)
    assert len(tokens) == 1
    assert ical.resolve_feed_token(a.db, tenant_id=a.tid, raw=tokens[0]) == a.pid
