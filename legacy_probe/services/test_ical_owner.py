"""Hosted canonical token rotation and RFC canaries, authored NOT RUN.

No legacy Person, new token model, metadata DDL or substituted token owner.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from uuid import uuid4

import pytest
from dotmac_kernel.session_runtime import resolve_database_runtime
from sqlalchemy import select, text, update
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.models.account_token import AccountToken
from app.models.platform_settings import PlatformSetting
from app.services import ical, settings_store
from app.services.security import hash_token
from legacy_probe.services.test_canonical_current_user import current_account
from legacy_probe.services.test_canonical_self_profile_locking import _bind, _wait_for_lock

assert current_account

POLICY = ("ical_feed_token_ttl_days", "ical_feed_uid_domain", "ical_feed_filename", "branding_name")


@pytest.fixture
def calendar_account(current_account):
    a = current_account
    saved = {key: a.admin.get(PlatformSetting, key) for key in POLICY}
    values = {key: None if row is None else row.value for key, row in saved.items()}
    try:
        yield a
    finally:
        a.db.rollback()
        a.admin.rollback()
        for key in POLICY:
            row = a.admin.get(PlatformSetting, key)
            if saved[key] is not None:
                a.admin.merge(PlatformSetting(key=key, value=values[key]))
            elif row is not None:
                a.admin.delete(row)
        a.admin.commit()


def test_rotation_multiuse_used_expired_tenant_and_rollback(calendar_account):
    a = calendar_account
    now = datetime.now(UTC)
    root = a.db.get_transaction()
    raw = ical.mint_feed_token(a.db, tenant_id=a.tid, person_id=a.pid, now=now)
    stored = a.db.scalars(select(AccountToken).where(AccountToken.token_hash == hash_token(raw))).one()
    assert raw != stored.token_hash and len(stored.token_hash) == 64
    assert stored.used_at is None
    assert ical.has_feed_token(a.db, tenant_id=a.tid, person_id=a.pid, now=now)
    for _ in range(2):
        assert ical.resolve_feed_token(a.db, tenant_id=a.tid, raw=raw, now=now) == a.pid
    assert ical.resolve_feed_token(a.db, tenant_id=uuid4(), raw=raw, now=now) is None
    assert ical.resolve_feed_token(a.db, tenant_id=a.tid, raw="", now=now) is None
    assert ical.resolve_feed_token(a.db, tenant_id=a.tid, raw=raw, now=stored.expires_at) is None
    stored.used_at = now
    a.db.flush()
    assert ical.resolve_feed_token(a.db, tenant_id=a.tid, raw=raw, now=now) is None
    assert not ical.has_feed_token(a.db, tenant_id=a.tid, person_id=a.pid, now=now)
    stored.used_at = None
    a.db.flush()
    replacement = ical.mint_feed_token(a.db, tenant_id=a.tid, person_id=a.pid, now=now)
    assert ical.resolve_feed_token(a.db, tenant_id=a.tid, raw=raw, now=now) is None
    assert ical.resolve_feed_token(a.db, tenant_id=a.tid, raw=replacement, now=now) == a.pid
    assert a.db.get_transaction() is root
    a.db.rollback()
    assert ical.resolve_feed_token(a.db, tenant_id=a.tid, raw=replacement, now=now) is None


def test_scalar_token_read_observes_remote_invalidation_and_rotation_rollback(calendar_account):
    a = calendar_account
    raw = ical.mint_feed_token(a.db, tenant_id=a.tid, person_id=a.pid)
    a.db.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    retained = a.db.scalars(select(AccountToken).where(AccountToken.token_hash == hash_token(raw))).one()
    assert retained.used_at is None
    a.admin.execute(update(AccountToken).where(AccountToken.id == retained.id).values(used_at=datetime.now(UTC)))
    a.admin.commit()
    assert retained.used_at is None  # Deliberately retained stale ORM observation.
    assert ical.resolve_feed_token(a.db, tenant_id=a.tid, raw=raw) is None
    assert not ical.has_feed_token(a.db, tenant_id=a.tid, person_id=a.pid)
    a.admin.execute(update(AccountToken).where(AccountToken.id == retained.id).values(used_at=None))
    a.admin.commit()
    replacement = ical.mint_feed_token(a.db, tenant_id=a.tid, person_id=a.pid)
    assert ical.resolve_feed_token(a.db, tenant_id=a.tid, raw=raw) is None
    a.db.rollback()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert ical.resolve_feed_token(a.db, tenant_id=a.tid, raw=raw) == a.pid
    assert ical.resolve_feed_token(a.db, tenant_id=a.tid, raw=replacement) is None


def test_concurrent_rotations_wait_on_actual_person_and_only_last_survives(calendar_account):
    a = calendar_account
    a.db.commit()
    factory = sessionmaker(bind=a.db.get_bind(), autoflush=False)
    locked, started, release = Event(), Event(), Event()
    backend, tokens = [], {}

    def leader():
        with factory() as db:
            _bind(db, a.tid)
            tokens["first"] = ical.mint_feed_token(db, tenant_id=a.tid, person_id=a.pid)
            locked.set()
            assert release.wait(10)
            db.commit()

    def follower():
        with factory() as db:
            _bind(db, a.tid)
            backend.append(db.scalar(text("SELECT pg_backend_pid()")))
            started.set()
            tokens["second"] = ical.mint_feed_token(db, tenant_id=a.tid, person_id=a.pid)
            db.commit()

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
    assert ical.resolve_feed_token(a.db, tenant_id=a.tid, raw=tokens["first"]) is None
    assert ical.resolve_feed_token(a.db, tenant_id=a.tid, raw=tokens["second"]) == a.pid


def test_custom_ttl_and_strict_store_policy(calendar_account):
    a = calendar_account
    settings_store.set_many(a.admin, {"ical_feed_token_ttl_days": "12"})
    a.admin.commit()
    now = datetime.now(UTC)
    raw = ical.mint_feed_token(a.db, tenant_id=a.tid, person_id=a.pid, now=now)
    expiry = a.db.scalar(select(AccountToken.expires_at).where(AccountToken.token_hash == hash_token(raw)))
    assert expiry == now + timedelta(days=12)
    for value in (True, False, 0, -1, 1.5, "bad", None):
        with pytest.raises(ValueError):
            Settings(ical_feed_token_ttl_days=value)
    for key, value in (("ical_feed_token_ttl_days", "0"), ("ical_feed_uid_domain", "x\rINJECT"),
                       ("ical_feed_filename", 'bad".ics'), ("ical_feed_filename", "../bad.ics")):
        with pytest.raises(ValueError):
            settings_store.set_many(a.db, {key: value})
    null_policy = a.admin.get(PlatformSetting, "ical_feed_uid_domain")
    if null_policy is None:
        a.admin.add(PlatformSetting(key="ical_feed_uid_domain", value=None))
    else:
        null_policy.value = None
    a.admin.flush()
    a.admin.commit()
    with pytest.raises(ValueError):
        settings_store.effective(a.db, require_store=True)


@pytest.mark.parametrize("line", ["a" * 74 + "🌍" * 80, "学" * 100, "e\u0301" * 100, "plain", ""])
def test_rfc_fold_roundtrip_all_unicode_and_octet_limit(line):
    folded = ical._fold(line)
    assert folded.replace("\r\n ", "") == line
    assert all(len(part.encode("utf-8")) <= 75 for part in folded.split("\r\n"))


def test_text_injection_uri_and_rfc_render():
    now = datetime(2026, 10, 19, tzinfo=UTC)
    body = ical.render_feed([{"uid": "stable@domain", "summary": "学" * 90 + "\rINJECT\nEND",
                             "starts_at": now, "url": "https://example.test/a,b;c\r\nINJECT"}],
                            calname="Name\r\nLine", now=now)
    unfolded = body.replace("\r\n ", "")
    assert "SUMMARY:" + "学" * 90 + "\\nINJECT\\nEND\r\n" in unfolded
    assert "X-WR-CALNAME:Name\\nLine\r\n" in unfolded
    assert "URL:https://example.test/a,b;cINJECT\r\n" in unfolded
    assert all(len(line.encode("utf-8")) <= 75 for line in body.split("\r\n"))
    assert "\r\nINJECT" not in body
