"""Pure logout handoff contracts; no engines, connections, or database fixtures."""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from dotmac_kernel import identity_storage, transactions
from sqlalchemy.dialects import postgresql

from app.services import web_auth


@dataclass(frozen=True)
class SessionRow:
    id: object
    party_id: object
    revoked_at: datetime | None = None
    updated_at: datetime | None = None


class LogoutSession:
    def __init__(self, rows):
        self.rows = iter(rows)
        self.queries = []
        self.events = []
        self.new, self.dirty, self.deleted = set(), set(), set()
        self.root = object()
        self.active_root = self.root
        self.inside_savepoint = False
        self.persisted_at = None

    def scalars(self, query):
        assert self.inside_savepoint
        assert self.active_root is self.root
        self.queries.append(query)
        self.events.append("query")
        return SimpleNamespace(first=lambda: next(self.rows))

    def refresh(self, row, *, attribute_names):
        assert self.inside_savepoint
        assert self.active_root is self.root
        assert attribute_names == ["revoked_at", "updated_at"]
        self.events.append(("refresh", row))
        object.__setattr__(row, "revoked_at", self.persisted_at)
        object.__setattr__(row, "updated_at", self.persisted_at)

    def flush(self, *args, **kwargs):
        pytest.fail("logout must not flush")

    def commit(self):
        pytest.fail("logout must not commit")

    def rollback(self):
        pytest.fail("logout must not roll back the caller root")


@pytest.fixture
def boundary(monkeypatch):
    calls = []

    @contextmanager
    def savepoint(db):
        assert not db.inside_savepoint
        db.events.append("enter")
        db.inside_savepoint = True
        try:
            yield
        except Exception:
            db.events.append("savepoint-error")
            raise
        finally:
            db.inside_savepoint = False
            db.events.append("exit")

    def owner(db, **kwargs):
        assert db.inside_savepoint
        assert db.active_root is db.root
        assert not (db.new or db.dirty or db.deleted)
        db.events.append("owner")
        calls.append(kwargs)
        db.persisted_at = kwargs["revoked_at"]
        return kwargs["session_ids"]

    monkeypatch.setattr(transactions, "conflict_savepoint", savepoint)
    monkeypatch.setattr(identity_storage, "revoke_party_sessions", owner)
    monkeypatch.setattr(web_auth, "hash_token", lambda token: "opaque-digest")
    return calls


def scenario(*, prior_revocation=None, candidate_revocation=None):
    tenant, party, selected = uuid4(), uuid4(), uuid4()
    candidate = SessionRow(selected, party, candidate_revocation)
    fresh = SessionRow(selected, party, prior_revocation)
    db = LogoutSession([candidate, SimpleNamespace(id=party), fresh])
    return db, tenant, party, selected, fresh


def test_logout_passes_only_fresh_selected_session_to_owner_then_refreshes(boundary):
    # Candidate state is stale: only the locked populate_existing query decides.
    db, tenant, party, selected, fresh = scenario(candidate_revocation=datetime.now(UTC))
    web_auth.revoke_session(db, tenant, "cookie")
    assert len(boundary) == 1
    call = boundary[0]
    assert call["tenant_id"] == tenant
    assert call["party_id"] == party
    assert call["session_ids"] == (selected,)
    assert call["revoked_at"].utcoffset() is not None
    assert fresh.revoked_at == fresh.updated_at == call["revoked_at"]
    assert db.events == ["enter", "query", "query", "query", "owner", ("refresh", fresh), "exit"]
    assert db.active_root is db.root

    compiled = [query.compile(dialect=postgresql.dialect()) for query in db.queries]
    sql = [str(query) for query in compiled]
    assert "FOR UPDATE" not in sql[0]
    assert "parties" in sql[1] and "FOR UPDATE" in sql[1]
    assert "auth_sessions" in sql[2] and "FOR UPDATE" in sql[2]
    for query in db.queries:
        assert query.get_execution_options()["populate_existing"] is True
    assert tenant in compiled[0].params.values()
    assert "opaque-digest" in compiled[0].params.values()
    assert set(compiled[1].params.values()) == {tenant, party}
    assert set(compiled[2].params.values()) == {tenant, party, selected, "opaque-digest"}


@pytest.mark.parametrize("token", [None, ""])
def test_absent_token_does_not_enter_savepoint_or_read(boundary, token):
    db = LogoutSession([])
    db.dirty.add(object())
    web_auth.revoke_session(db, uuid4(), token)
    assert db.events == [] and boundary == []


@pytest.mark.parametrize("missing_index", [0, 1, 2])
def test_missing_candidate_party_or_locked_session_is_noop(boundary, missing_index):
    db, tenant, _, _, _ = scenario()
    rows = list(db.rows)
    rows[missing_index] = None
    db.rows = iter(rows)
    web_auth.revoke_session(db, tenant, "cookie")
    assert boundary == []
    assert len(db.queries) == missing_index + 1
    assert db.events[-1] == "exit"


def test_fresh_prior_revocation_is_preserved_without_owner_or_refresh(boundary):
    prior = datetime(2020, 1, 1, tzinfo=UTC)
    db, tenant, _, _, fresh = scenario(prior_revocation=prior)
    web_auth.revoke_session(db, tenant, "cookie")
    assert boundary == []
    assert fresh.revoked_at == prior
    assert db.events == ["enter", "query", "query", "query", "exit"]


@pytest.mark.parametrize("pending", ["new", "dirty", "deleted"])
def test_pending_caller_work_refuses_before_savepoint_preflush(boundary, pending):
    db, tenant, _, _, _ = scenario()
    getattr(db, pending).add(object())
    with pytest.raises(transactions.ConflictSavepointProtocolError, match="clean pending state"):
        web_auth.revoke_session(db, tenant, "cookie")
    assert db.events == [] and boundary == []


def test_owner_error_propagates_through_savepoint_without_root_rollback(boundary, monkeypatch):
    error = transactions.ConflictSavepointProtocolError("owner refusal")

    def refuse(*args, **kwargs):
        raise error

    monkeypatch.setattr(identity_storage, "revoke_party_sessions", refuse)
    db, tenant, _, _, fresh = scenario()
    with pytest.raises(transactions.ConflictSavepointProtocolError) as caught:
        web_auth.revoke_session(db, tenant, "cookie")
    assert caught.value is error
    assert fresh.revoked_at is None
    assert db.events[-2:] == ["savepoint-error", "exit"]
    assert db.active_root is db.root


def test_empty_persisted_outcome_does_not_refresh_unmodified_row(boundary, monkeypatch):
    monkeypatch.setattr(identity_storage, "revoke_party_sessions", lambda *args, **kwargs: ())
    db, tenant, _, _, fresh = scenario()
    web_auth.revoke_session(db, tenant, "cookie")
    assert fresh.revoked_at is None
    assert db.events == ["enter", "query", "query", "query", "exit"]
