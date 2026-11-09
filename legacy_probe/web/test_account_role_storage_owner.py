"""Pure cookie/role orchestration; physical locks and RLS remain hosted proof."""

import ast
import inspect
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from dotmac_kernel import identity_storage
from dotmac_kernel.models import PartyType
from dotmac_kernel.transactions import conflict_savepoint
from fastapi import HTTPException, Request

from app.services import web_account_authority as authority
from app.services.security import hash_token
from app.web import accounts


@pytest.fixture
def role_case(monkeypatch):
    tenant_id, actor_id, target_id, session_id = uuid4(), uuid4(), uuid4(), uuid4()
    token = "pure-cookie-proof"
    roles = {slug: SimpleNamespace(id=uuid4(), slug=slug) for slug in ("student", "instructor", "admin", "custom")}
    events = []
    state = SimpleNamespace(inside=False, materialized=False, actor_valid=True, admin=True, final_failure=None)
    request = Request({"type": "http", "headers": [(b"cookie", f"session={token}".encode())]})
    request.state.tenant = SimpleNamespace(id=tenant_id)
    actor = SimpleNamespace(id=actor_id, tenant_id=tenant_id)
    parties = tuple(SimpleNamespace(id=pid, party_type=PartyType.person) for pid in (actor_id, target_id))
    grants = tuple(
        identity_storage.GrantFact(uuid4(), tenant_id, target_id, roles[slug].id)
        for slug in ("student", "custom")
    )
    session = SimpleNamespace(id=session_id, party_id=actor_id)

    class Db:
        new = ()
        dirty = ()
        deleted = ()

        def connection(self):
            assert state.inside
            state.materialized = True
            events.append("materialize")

        def flush(self):
            assert state.inside
            events.append("flush")

        def scalars(self, statement):
            params = statement.compile().params
            assert tenant_id in params.values() and hash_token(token) in params.values()
            assert statement.get_execution_options()["populate_existing"]
            if "FOR UPDATE" in str(statement):
                assert session_id in params.values() and actor_id in params.values()
                events.append("lock cookie")
            else:
                events.append("identify cookie")
            return SimpleNamespace(first=lambda: session)

    db = Db()

    def slugs(actual_db, tid, pid):
        assert actual_db is db and tid == tenant_id and pid == actor_id
        events.append("preflight")
        return {"admin"}

    def lock_parties(actual_db, **scope):
        assert actual_db is db and scope == dict(tenant_id=tenant_id, party_ids=(actor_id, case.target_id))
        events.append("all parties")
        return case.parties

    def subordinate(actual_db, **scope):
        assert actual_db is db and scope["tenant_id"] == tenant_id
        events.append("subordinate")
        return object()

    def lock_roles(actual_db, **scope):
        assert actual_db is db and scope == dict(tenant_id=tenant_id)
        events.append("actor roles")
        return tuple(roles.values())

    def actor_grants(actual_db, **scope):
        assert actual_db is db and scope == dict(tenant_id=tenant_id, party_ids=(actor_id,))
        events.append("actor grants")
        return (SimpleNamespace(role_id=roles["admin" if state.admin else "student"].id),)

    def current(request, actual_db):
        assert actual_db is db and request.cookies.get("session") == token
        events.append("fresh cookie/account/time")
        if state.final_failure:
            raise state.final_failure
        if not state.actor_valid:
            raise HTTPException(status_code=303, headers={"Location": "/login"})
        return actor

    def ensure_roles(actual_db, tid):
        assert actual_db is db and tid == tenant_id and state.inside
        events.append("roles")
        return roles

    def lock_grants(actual_db, **scope):
        assert actual_db is db and scope == dict(tenant_id=tenant_id, party_ids=(case.target_id,))
        events.append("target grants")
        return case.grants

    @contextmanager
    def savepoint(actual_db):
        assert actual_db is db
        events.append("savepoint")
        state.inside = True
        try:
            yield
        except BaseException:
            events.append("rollback savepoint")
            raise
        else:
            events.append("release savepoint")
        finally:
            state.inside = False

    def remove(actual_db, **scope):
        assert actual_db is db and state.inside and state.materialized
        assert scope == dict(
            tenant_id=tenant_id, party_id=case.target_id,
            expected=tuple(g for g in case.grants if g.role_id == roles["student"].id),
        )
        events.append("remove")
        if case.failure:
            raise case.failure
        return scope["expected"]

    def ensure(actual_db, **scope):
        assert actual_db is db and state.inside and state.materialized
        assert scope == dict(
            tenant_id=tenant_id, party_id=case.target_id, role_id=roles["instructor"].id,
            new_grant_id=None, created_at=None, updated_at=None,
        )
        events.append("ensure")

    monkeypatch.setattr(accounts, "_role_slugs", slugs)
    monkeypatch.setattr(authority, "lock_parties", lock_parties)
    monkeypatch.setattr(authority, "lock_person", subordinate)
    monkeypatch.setattr(authority, "lock_profile", subordinate)
    monkeypatch.setattr(authority, "lock_roles", lock_roles)
    monkeypatch.setattr(authority, "lock_grants", actor_grants)
    monkeypatch.setattr(authority, "require_web_user", current)
    monkeypatch.setattr(authority, "role_slugs", lambda *args: {"admin"} if state.admin else {"student"})
    monkeypatch.setattr(accounts, "ensure_roles", ensure_roles)
    monkeypatch.setattr(accounts, "lock_grants", lock_grants)
    monkeypatch.setattr(accounts, "conflict_savepoint", savepoint)
    monkeypatch.setattr(accounts, "remove_grants", remove)
    monkeypatch.setattr(accounts, "ensure_grant", ensure)
    case = SimpleNamespace(
        tenant_id=tenant_id, actor=actor, target_id=target_id, request=request, db=db,
        events=events, roles=roles, grants=grants, failure=None, state=state, parties=parties,
    )
    return case


@pytest.mark.parametrize("htmx", [False, True])
def test_role_change_removes_exact_observed_standard_subset_preserving_custom(role_case, htmx):
    c = role_case
    if htmx:
        c.request = Request({"type": "http", "headers": [(b"cookie", b"session=pure-cookie-proof"), (b"hx-request", b"true")]})
        c.request.state.tenant = SimpleNamespace(id=c.tenant_id)
    response = accounts.users_role(c.target_id, c.request, role="instructor", actor=c.actor, db=c.db)
    assert c.events == [
        "preflight", "savepoint", "materialize", "identify cookie", "all parties",
        *(4 * ["subordinate"]), "actor roles", "actor grants", "lock cookie", "fresh cookie/account/time",
        "roles", "target grants", "fresh cookie/account/time", "remove", "ensure", "flush", "release savepoint",
    ]
    assert response.status_code == (200 if htmx else 303)
    assert response.headers["hx-redirect" if htmx else "location"] == "/admin/users"


def test_existing_target_grant_is_preserved_without_recreation(role_case):
    c = role_case
    c.grants += (identity_storage.GrantFact(uuid4(), c.tenant_id, c.target_id, c.roles["instructor"].id),)
    accounts.users_role(c.target_id, c.request, role="instructor", actor=c.actor, db=c.db)
    assert "remove" in c.events and "ensure" not in c.events
    assert c.events[-2:] == ["flush", "release savepoint"]


def test_owner_failure_propagates_and_prevents_grant_or_redirect(role_case):
    c = role_case
    c.failure = RuntimeError("owner refused exact cohort")
    with pytest.raises(RuntimeError) as observed:
        accounts.users_role(c.target_id, c.request, role="instructor", actor=c.actor, db=c.db)
    assert observed.value is c.failure
    assert c.events[-1] == "rollback savepoint"
    assert "ensure" not in c.events and "flush" not in c.events


def test_admin_authority_rechecked_under_locks(role_case):
    c = role_case
    c.state.admin = False
    with pytest.raises(HTTPException) as observed:
        accounts.users_role(c.target_id, c.request, role="instructor", actor=c.actor, db=c.db)
    assert observed.value.status_code == 403
    assert "roles" not in c.events and "remove" not in c.events


@pytest.mark.parametrize("role,admin,status_code", [("instructor", False, 403), ("custom", True, 400)])
def test_invalid_role_or_nonadmin_fails_before_locks(role_case, monkeypatch, role, admin, status_code):
    c = role_case
    monkeypatch.setattr(accounts, "_role_slugs", lambda *args: {"admin"} if admin else {"instructor"})
    with pytest.raises(HTTPException) as observed:
        accounts.users_role(c.target_id, c.request, role=role, actor=c.actor, db=c.db)
    assert observed.value.status_code == status_code
    assert c.events == []


def test_self_demotion_denied_before_role_catalog_or_storage(role_case):
    c = role_case
    c.target_id = c.actor.id
    c.parties = (c.parties[0],)
    with pytest.raises(HTTPException) as observed:
        accounts.users_role(c.target_id, c.request, role="student", actor=c.actor, db=c.db)
    assert observed.value.status_code == 400
    assert observed.value.detail == "You cannot remove your own admin role here"
    assert "roles" not in c.events and "remove" not in c.events


@pytest.mark.parametrize("target_kind", ["missing", "organization"])
def test_missing_or_nonperson_target_is_404_before_storage(role_case, target_kind):
    c = role_case
    c.parties = (c.parties[0],)
    if target_kind == "organization":
        c.parties += (SimpleNamespace(id=c.target_id, party_type=PartyType.organization),)
    with pytest.raises(HTTPException) as observed:
        accounts.users_role(c.target_id, c.request, role="instructor", actor=c.actor, db=c.db)
    assert observed.value.status_code == 404
    assert observed.value.detail == "User not found"
    assert "roles" not in c.events and "remove" not in c.events


def test_missing_cookie_refuses_even_when_dependency_projection_says_admin(role_case):
    c = role_case
    request = Request({"type": "http", "headers": [], "state": {"tenant": SimpleNamespace(id=c.tenant_id)}})
    with pytest.raises(HTTPException) as observed:
        accounts.users_role(c.target_id, request, role="instructor", actor=c.actor, db=c.db)
    assert observed.value.status_code == 303 and observed.value.headers == {"Location": "/login"}
    assert "all parties" not in c.events and "remove" not in c.events


def test_route_uses_public_storage_and_has_no_local_grant_writer():
    assert accounts.remove_grants is identity_storage.remove_grants
    assert accounts.ensure_grant is identity_storage.ensure_grant
    assert authority.lock_parties is identity_storage.lock_parties
    assert accounts.lock_grants is identity_storage.lock_grants
    assert accounts.conflict_savepoint is conflict_savepoint
    route = ast.parse(inspect.getsource(accounts.users_role))
    assert not any(
        isinstance(node, ast.Call) and (
            isinstance(node.func, ast.Name) and node.func.id == "PartyRoleGrant"
            or isinstance(node.func, ast.Attribute) and node.func.attr in {"add", "delete", "commit", "rollback"}
        ) for node in ast.walk(route)
    )
