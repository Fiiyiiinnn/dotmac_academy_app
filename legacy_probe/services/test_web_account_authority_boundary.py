"""Pure request/lock ordering; doubles are not PostgreSQL authority evidence."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services import web_account_authority as authority
from app.web import accounts
from legacy_probe.web.test_account_role_storage_owner import role_case

assert role_case


def test_complete_party_set_precedes_subordinate_locks_and_cookie_refresh(role_case):
    c = role_case
    actor_id, target = authority.lock_web_admin_write(
        c.db, c.request, target_id=c.target_id, forbidden_detail="Forbidden",
    )
    assert actor_id == c.actor.id and target.id == c.target_id
    assert c.events[:2] == ["identify cookie", "all parties"]
    assert c.events.index("all parties") < c.events.index("subordinate")
    assert c.events.index("actor grants") < c.events.index("lock cookie")
    assert c.events[-1] == "fresh cookie/account/time"


@pytest.mark.parametrize("component", ["person", "profile"])
def test_missing_genuine_actor_component_is_invalid_login(role_case, monkeypatch, component):
    c = role_case
    monkeypatch.setattr(authority, f"lock_{component}", lambda *args, **kwargs: None)
    with pytest.raises(HTTPException) as observed:
        authority.lock_web_admin_write(c.db, c.request, target_id=c.target_id, forbidden_detail="Forbidden")
    assert observed.value.status_code == 303
    assert "actor roles" not in c.events


def test_current_account_failure_has_login_precedence_over_lost_admin(role_case):
    c = role_case
    c.state.actor_valid = False
    c.state.admin = False
    with pytest.raises(HTTPException) as observed:
        authority.lock_web_admin_write(c.db, c.request, target_id=c.target_id, forbidden_detail="Forbidden")
    assert observed.value.status_code == 303


def test_changed_cookie_identity_is_invalid_even_when_new_actor_is_admin(role_case, monkeypatch):
    c = role_case
    monkeypatch.setattr(authority, "require_web_user", lambda *args: SimpleNamespace(id=c.target_id, tenant_id=c.tenant_id))
    with pytest.raises(HTTPException) as observed:
        authority.lock_web_admin_write(c.db, c.request, target_id=c.target_id, forbidden_detail="Forbidden")
    assert observed.value.status_code == 303


def test_role_final_cookie_check_follows_target_grant_wait_and_prevents_mutation(role_case, monkeypatch):
    c = role_case
    real = accounts.lock_grants

    def waited(*args, **kwargs):
        result = real(*args, **kwargs)
        c.state.final_failure = HTTPException(status_code=303, headers={"Location": "/login"})
        return result

    monkeypatch.setattr(accounts, "lock_grants", waited)
    with pytest.raises(HTTPException) as observed:
        accounts.users_role(c.target_id, c.request, role="instructor", actor=c.actor, db=c.db)
    assert observed.value.status_code == 303
    assert c.events[-3:] == ["target grants", "fresh cookie/account/time", "rollback savepoint"]
    assert "remove" not in c.events and "ensure" not in c.events and "flush" not in c.events


def test_role_final_admin_check_follows_catalog_wait(role_case, monkeypatch):
    c = role_case
    real = accounts.ensure_roles

    def waited(*args):
        result = real(*args)
        c.state.admin = False
        return result

    monkeypatch.setattr(accounts, "ensure_roles", waited)
    with pytest.raises(HTTPException) as observed:
        accounts.users_role(c.target_id, c.request, role="instructor", actor=c.actor, db=c.db)
    assert observed.value.status_code == 403
    assert observed.value.detail == "Only admins can change roles"
    assert "remove" not in c.events and c.events[-1] == "rollback savepoint"
