"""Pure status ownership/order checks; no physical SAVEPOINT or RLS claim."""

from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from dotmac_kernel import identity_storage
from dotmac_kernel.transactions import ConflictSavepointProtocolError

from app.services import canonical_lifecycle as lifecycle


@pytest.fixture
def status_case(monkeypatch):
    tid, pid = uuid4(), uuid4()
    profile = SimpleNamespace(status="active", updated_at=None)
    selected = [SimpleNamespace(id=uuid4(), revoked_at=None, updated_at=None) for _ in range(2)]
    events = []
    state = SimpleNamespace(inside=False, failure=None, selected=selected, expected_status="suspended")

    class Db:
        new = ()
        dirty = ()
        deleted = ()

        def get_transaction(self):
            return root

        def connection(self):
            assert state.inside
            events.append("materialize")

        def scalars(self, statement):
            assert state.inside
            assert "ORDER BY auth_sessions.id" in str(statement)
            events.append("target sessions")
            return SimpleNamespace(all=lambda: state.selected)

        def refresh(self, row, *, attribute_names):
            assert state.inside and attribute_names == ["revoked_at", "updated_at"]
            events.append("refresh")

        def flush(self):
            assert state.inside and profile.status == state.expected_status
            events.append("profile flush")

    db, root = Db(), object()

    @contextmanager
    def savepoint(actual):
        assert actual is db
        events.append("savepoint")
        state.inside = True
        try:
            yield
        except BaseException:
            events.append("rollback")
            raise
        finally:
            state.inside = False

    def owner(actual, **values):
        assert actual is db and state.inside
        assert profile.status == "active" and profile.updated_at is None
        assert values == dict(
            tenant_id=tid, party_id=pid, session_ids=tuple(s.id for s in state.selected), revoked_at=when,
        )
        events.append("canonical owner")
        if state.failure:
            raise state.failure
        return values["session_ids"]

    monkeypatch.setattr(lifecycle, "conflict_savepoint", savepoint)
    monkeypatch.setattr(lifecycle, "_lock_account", lambda *args, **kwargs: (SimpleNamespace(id=pid), profile))
    monkeypatch.setattr(lifecycle, "revoke_party_sessions", owner)
    when = datetime(2030, 1, 1, tzinfo=UTC)
    return SimpleNamespace(db=db, tid=tid, pid=pid, profile=profile, state=state, events=events, when=when)


@pytest.mark.parametrize("empty", [False, True])
def test_status_uses_exact_public_owner_before_profile_and_refreshes_aliases(status_case, empty):
    c = status_case
    if empty:
        c.state.selected = []
    assert lifecycle.set_account_status(
        c.db, tenant_id=c.tid, person_id=c.pid, status="suspended", now=c.when,
    ) is c.profile
    assert c.events == ["savepoint", "materialize", "target sessions", "canonical owner", *([] if empty else ["refresh", "refresh"]), "profile flush"]
    assert c.profile.updated_at == c.when


def test_canonical_failure_prevents_product_mutation(status_case):
    c = status_case
    c.state.failure = RuntimeError("canonical owner refused")
    with pytest.raises(RuntimeError, match="canonical owner refused"):
        lifecycle.set_account_status(c.db, tenant_id=c.tid, person_id=c.pid, status="suspended", now=c.when)
    assert c.profile.status == "active" and c.profile.updated_at is None
    assert c.events[-1] == "rollback"
    assert "profile flush" not in c.events and "refresh" not in c.events


@pytest.mark.parametrize("pending", ["new", "dirty", "deleted"])
def test_pending_state_refuses_before_savepoint_preflush(status_case, pending):
    c = status_case
    setattr(c.db, pending, (object(),))
    with pytest.raises(ConflictSavepointProtocolError):
        lifecycle.set_account_status(c.db, tenant_id=c.tid, person_id=c.pid, status="suspended")
    assert c.events == [] and c.profile.status == "active"


def test_owner_is_actual_public_kernel_function():
    assert lifecycle.revoke_party_sessions is identity_storage.revoke_party_sessions


def test_reactivation_has_no_session_lookup_or_dml(status_case):
    c = status_case
    c.profile.status = "suspended"
    c.state.expected_status = "active"
    lifecycle.set_account_status(c.db, tenant_id=c.tid, person_id=c.pid, status="active", now=c.when)
    assert c.events == ["savepoint", "materialize", "profile flush"]


def test_web_status_final_authentication_follows_target_session_wait(status_case, monkeypatch):
    from fastapi import HTTPException, Request

    c = status_case
    request = Request({"type": "http", "headers": [], "state": {"tenant": SimpleNamespace(id=c.tid)}})
    actor_id = uuid4()
    target = SimpleNamespace(id=c.pid)
    monkeypatch.setattr(lifecycle, "lock_web_admin_write", lambda *args, **kwargs: (actor_id, target))
    original = c.db.scalars

    def scalars(statement):
        if "academy_account_profiles" in str(statement):
            c.events.append("target profile")
            return SimpleNamespace(first=lambda: c.profile)
        return original(statement)

    monkeypatch.setattr(c.db, "scalars", scalars)

    def expired(*args, **kwargs):
        assert kwargs == dict(actor_id=actor_id, forbidden_detail="Forbidden")
        assert c.events[-1] == "target sessions"
        c.events.append("final cookie time")
        raise HTTPException(status_code=303, headers={"Location": "/login"})

    monkeypatch.setattr(lifecycle, "revalidate_web_admin_write", expired)
    monkeypatch.setattr(lifecycle, "_require_recovered_root", lambda *args: c.events.append("recovered root"))
    with pytest.raises(HTTPException) as observed:
        lifecycle.set_web_account_status(c.db, request, person_id=c.pid, status="suspended", forbidden_detail="Forbidden")
    assert observed.value.status_code == 303
    assert c.events[-4:] == ["target sessions", "final cookie time", "rollback", "recovered root"]
    assert "canonical owner" not in c.events and c.profile.status == "active"


def test_both_status_routes_use_request_owner_and_preserve_responses(monkeypatch):
    from fastapi import Request

    from app.web import accounts, instructor

    tid, pid = uuid4(), uuid4()
    request = Request({"type": "http", "headers": [], "state": {"tenant": SimpleNamespace(id=tid)}})
    calls = []
    db = SimpleNamespace(new=(), dirty=(), deleted=())

    def owner(actual, incoming, **kwargs):
        assert actual is db and incoming is request and kwargs["person_id"] == pid
        calls.append(kwargs)

    monkeypatch.setattr(accounts, "_role_slugs", lambda *args: {"admin"})
    monkeypatch.setattr(accounts, "set_web_account_status", owner)
    monkeypatch.setattr(instructor, "set_web_account_status", owner)
    response = accounts.users_status(pid, request, status_value="suspended", actor=SimpleNamespace(id=uuid4()), db=db)
    assert response.status_code == 200 and b"Account suspended" in response.body
    response = instructor.change_account_status(pid, request, status_value="active", db=db)
    assert response.status_code == 303 and response.headers["location"] == "/instructor/cohorts"
    assert [call["forbidden_detail"] for call in calls] == ["Only admins can manage accounts", "Forbidden"]


def test_web_timestamp_observed_after_final_target_session_wait(status_case, monkeypatch):
    from fastapi import Request

    c = status_case
    req = Request({"type": "http", "headers": [], "state": {"tenant": SimpleNamespace(id=c.tid)}})
    actor_id = uuid4()
    monkeypatch.setattr(lifecycle, "lock_web_admin_write", lambda *args, **kwargs: (actor_id, SimpleNamespace(id=c.pid)))
    original = c.db.scalars

    def scalars(statement):
        if "academy_account_profiles" in str(statement):
            return SimpleNamespace(first=lambda: c.profile)
        return original(statement)

    monkeypatch.setattr(c.db, "scalars", scalars)

    def proof(*args, **kwargs):
        assert c.events[-1] == "target sessions"
        c.events.append("final proof")

    class Clock:
        @staticmethod
        def now(tz):
            assert tz is UTC and c.events[-1] == "final proof"
            c.events.append("operation clock")
            return c.when

    monkeypatch.setattr(lifecycle, "revalidate_web_admin_write", proof)
    monkeypatch.setattr(lifecycle, "datetime", Clock)
    lifecycle.set_web_account_status(c.db, req, person_id=c.pid, status="suspended", forbidden_detail="Forbidden")
    assert c.events.index("target sessions") < c.events.index("final proof") < c.events.index("operation clock")
    assert c.events.index("operation clock") < c.events.index("canonical owner")
