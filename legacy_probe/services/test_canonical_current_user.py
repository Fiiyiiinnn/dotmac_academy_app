"""Hosted genuine current-request authority reads, never writer authorization."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from dotmac_kernel.models import AuthSession, Party, PartyPerson, PartyRoleGrant, Role
from dotmac_kernel.session_runtime import resolve_database_runtime
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import select, text, update
from starlette.requests import Request

from app.api import auth as api_auth
from app.api import deps
from app.models.identity_extensions import AcademyAccountProfile
from app.services import web_auth
from app.services.credential_authentication import sign_in
from app.services.security import hash_token, issue_access_token
from legacy_probe.services.test_canonical_sign_in import sign_in_account

assert sign_in_account


def _request(tenant, token=None):
    headers = [] if token is None else [(b"cookie", f"session={token}".encode())]
    return Request({"type": "http", "headers": headers, "state": {"tenant": tenant}})


def _bearer(token, scheme="Bearer"):
    return HTTPAuthorizationCredentials(scheme=scheme, credentials=token)


@pytest.fixture
def current_account(request):
    import os

    assert os.environ.get("CI") == "true", "canonical guard canaries are hosted only"
    assert os.getenv("TEST_DATABASE_URL"), "canonical app_user DSN is required"
    assert os.getenv("TEST_MIGRATION_DATABASE_URL"), "canonical migration DSN is required"
    request.getfixturevalue("app_client")
    db, tid, pid, email, cid = request.getfixturevalue("sign_in_account")
    result = sign_in(db, tenant_id=tid, email=email, password="chosen-password9")
    assert result is not None
    db.commit()  # Test setup ends the login writer lock before independent changes.
    resolve_database_runtime().set_tenant(db, tid)
    assert db.scalar(text("SELECT current_user")) == "app_user"
    return SimpleNamespace(
        db=db,
        admin=request.getfixturevalue("admin_session"),
        tenant=request.getfixturevalue("tenant_a"),
        tid=tid,
        pid=pid,
        email=email,
        credential_id=cid,
        token=result.session_token,
        session_id=result.session_id,
        login_snapshot=result.account,
    )


def _web(account, token=None):
    return web_auth._current_person(account.db, account.tid, account.token if token is None else token)


def _api(account, token=None):
    return deps.require_user_auth(
        _request(account.tenant), _bearer(account.token if token is None else token), account.db
    )


def _assert_unauthorized(account):
    assert _web(account) is None
    with pytest.raises(HTTPException) as caught:
        _api(account)
    assert caught.value.status_code == 401 and caught.value.detail == "Unauthorized"


def test_live_immutable_projection_same_genuine_party_and_me_fields(current_account):
    a = current_account
    root = a.db.get_transaction()
    for account in (_web(a), _api(a)):
        assert account.id == a.pid and account.tenant_id == a.tid
        assert account.email == a.email and account.first_name == "Sign" and account.last_name == "In"
        assert account.status == "active"
        with pytest.raises((AttributeError, TypeError)):
            account.email = "mutated@a.edu"
        response = api_auth.me(account)
        assert response.model_dump() == {
            "id": a.pid,
            "email": a.email,
            "first_name": "Sign",
            "last_name": "In",
            "tenant_id": a.tid,
        }
    assert a.db.get_transaction() is root
    assert a.db.scalar(text("SELECT current_setting('app.current_tenant')")) == str(a.tid)


@pytest.mark.parametrize(
    "mutation", ["suspended", "inactive", "revoked", "expired", "profile_deleted", "subtype_deleted"]
)
def test_independent_committed_state_refuses_stale_identity_map(current_account, mutation):
    a = current_account
    cached = (
        a.db.get(AuthSession, a.session_id),
        a.db.get(Party, a.pid),
        a.db.get(PartyPerson, a.pid),
        a.db.get(AcademyAccountProfile, a.pid),
    )
    assert _web(a) is not None and _api(a) is not None
    if mutation == "suspended":
        a.admin.execute(
            update(AcademyAccountProfile).where(AcademyAccountProfile.party_id == a.pid).values(status="suspended")
        )
    elif mutation == "inactive":
        a.admin.execute(update(Party).where(Party.id == a.pid).values(is_active=False))
    elif mutation == "revoked":
        a.admin.execute(update(AuthSession).where(AuthSession.id == a.session_id).values(revoked_at=datetime.now(UTC)))
    elif mutation == "expired":
        a.admin.execute(
            update(AuthSession)
            .where(AuthSession.id == a.session_id)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    else:
        a.admin.delete(a.admin.get(AcademyAccountProfile if mutation == "profile_deleted" else PartyPerson, a.pid))
    a.admin.commit()
    assert cached[0].revoked_at is None and cached[1].is_active and cached[3].status == "active"
    _assert_unauthorized(a)
    # A successful login snapshot remains unchanged but cannot authorize a request.
    assert a.login_snapshot.status == "active"


def test_live_names_and_email_refresh_instead_of_returning_login_snapshot(current_account):
    a = current_account
    cached_party, cached_person = a.db.get(Party, a.pid), a.db.get(PartyPerson, a.pid)
    email = f"updated-{uuid4().hex}@a.edu"
    a.admin.execute(update(Party).where(Party.id == a.pid).values(email=email))
    a.admin.execute(update(PartyPerson).where(PartyPerson.party_id == a.pid).values(first_name="Current"))
    a.admin.commit()
    assert cached_party.email == a.email and cached_person.first_name == "Sign"
    for account in (_web(a), _api(a)):
        assert account.email == email and account.first_name == "Current"
    assert cached_party.email == email and cached_person.first_name == "Current"
    assert a.login_snapshot.email == a.email and a.login_snapshot.first_name == "Sign"


def test_cookie_transport_uses_session_hmac_without_jwt_requirement(current_account):
    a = current_account
    opaque = uuid4().hex + uuid4().hex
    row = AuthSession(
        tenant_id=a.tid,
        party_id=a.pid,
        token_hash=hash_token(opaque),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    a.db.add(row)
    a.db.flush()
    assert web_auth._current_person(a.db, a.tid, opaque).id == a.pid
    with pytest.raises(HTTPException) as caught:
        _api(a, opaque)
    assert caught.value.status_code == 401


@pytest.mark.parametrize("change", ["wrongtenantclaim", "wrongsub", "unpersisted", "tampered"])
def test_api_jwt_session_subject_and_tenant_binding(current_account, change):
    a = current_account
    token, expires_at = issue_access_token(
        uuid4() if change == "wrongsub" else a.pid, uuid4() if change == "wrongtenantclaim" else a.tid
    )
    if change != "unpersisted":
        a.db.add(AuthSession(tenant_id=a.tid, party_id=a.pid, token_hash=hash_token(token), expires_at=expires_at))
        a.db.flush()
    if change == "tampered":
        token += "tampered"
    with pytest.raises(HTTPException) as caught:
        _api(a, token)
    assert caught.value.status_code == 401


def test_wrong_request_tenant_never_resolves_cookie_or_bearer(current_account, tenant_b):
    a = current_account
    assert web_auth._current_person(a.db, tenant_b.id, a.token) is None
    with pytest.raises(HTTPException) as caught:
        deps.require_user_auth(_request(tenant_b), _bearer(a.token), a.db)
    assert caught.value.status_code == 401


def test_web_required_optional_and_missing_tenant_contracts(current_account):
    a = current_account
    assert web_auth.optional_web_user(_request(a.tenant), a.db) is None
    with pytest.raises(HTTPException) as caught:
        web_auth.require_web_user(_request(a.tenant), a.db)
    assert caught.value.status_code == 303 and caught.value.headers == {"Location": "/login"}
    assert web_auth.require_web_user(_request(a.tenant, a.token), a.db).id == a.pid
    assert web_auth.optional_web_user(_request(a.tenant, a.token), a.db).id == a.pid
    with pytest.raises(HTTPException) as caught:
        deps.require_user_auth(_request(None), None, a.db)
    assert caught.value.status_code == 404 and caught.value.detail == "Tenant not found"


@pytest.mark.parametrize("grant_slug", [None, "student", "instructor", "admin"])
def test_web_role_requested_or_admin_api_role_exact_and_live_grants(current_account, grant_slug):
    a = current_account
    if grant_slug is not None:
        role = a.admin.scalars(select(Role).where(Role.tenant_id == a.tid, Role.slug == grant_slug)).first()
        if role is None:
            role = Role(tenant_id=a.tid, slug=grant_slug, name=grant_slug)
            a.admin.add(role)
            a.admin.flush()
        a.admin.add(PartyRoleGrant(tenant_id=a.tid, party_id=a.pid, role_id=role.id))
        a.admin.commit()
    live = _web(a)
    web_guard, api_guard = web_auth.require_web_role("instructor"), deps.require_role("instructor")
    for guard, allowed in ((web_guard, grant_slug in ("instructor", "admin")), (api_guard, grant_slug == "instructor")):
        if allowed:
            assert guard(_request(a.tenant, a.token), live, a.db).id == a.pid
        else:
            with pytest.raises(HTTPException) as caught:
                guard(_request(a.tenant, a.token), live, a.db)
            assert caught.value.status_code == 403 and caught.value.detail == "Forbidden"
    if grant_slug in ("instructor", "admin"):
        grant = a.db.scalars(select(PartyRoleGrant).where(PartyRoleGrant.party_id == a.pid)).one()
        a.admin.delete(a.admin.get(PartyRoleGrant, grant.id))
        a.admin.commit()
        with pytest.raises(HTTPException) as caught:
            web_guard(_request(a.tenant, a.token), live, a.db)
        assert caught.value.status_code == 403


def test_request_actor_projection_is_actual_party_but_not_writer_authority(current_account):
    a = current_account
    actor = _api(a)
    assert a.db.get(Party, actor.id).id == actor.id
    assert actor.tenant_id == a.tid
    assert not hasattr(actor, "session_token") and not hasattr(actor, "password_hash")
    # A guard result has no grant-shaped capability or assignment-audit proof.
    assert not hasattr(actor, "authority_evidence_ref") and not hasattr(actor, "actor_type")
    a.admin.execute(
        update(AcademyAccountProfile).where(AcademyAccountProfile.party_id == a.pid).values(status="suspended")
    )
    a.admin.commit()
    assert actor.status == "active"  # immutable data cannot turn into enduring authorization
    _assert_unauthorized(a)


@pytest.mark.parametrize("transition", ["status", "reset"])
def test_actual_canonical_lifecycle_writer_invalidates_request_session(current_account, transition):
    from app.services import canonical_lifecycle

    a = current_account
    before = _api(a)
    if transition == "status":
        canonical_lifecycle.set_account_status(a.db, tenant_id=a.tid, person_id=a.pid, status="suspended")
        assert a.db.get(Party, a.pid).is_active is True
    else:
        raw = canonical_lifecycle.request_password_reset(a.db, tenant_id=a.tid, email=a.email)
        assert raw is not None
        canonical_lifecycle.reset_password(a.db, tenant_id=a.tid, raw=raw, new_password="replacement9")
    _assert_unauthorized(a)
    assert a.db.get(AuthSession, a.session_id).revoked_at is not None
    assert before.status == "active"  # a past read does not authorize the next request
    if transition == "status":
        canonical_lifecycle.set_account_status(a.db, tenant_id=a.tid, person_id=a.pid, status="active")
        _assert_unauthorized(a)  # reactivation cannot resurrect the old token
    fresh = sign_in(
        a.db, tenant_id=a.tid, email=a.email, password="replacement9" if transition == "reset" else "chosen-password9"
    )
    assert fresh is not None
    assert _api(a, fresh.session_token).id == a.pid
    assert _web(a, fresh.session_token).id == a.pid


def test_session_expiry_equality_is_rejected_by_both_transports(current_account, monkeypatch):
    a = current_account
    at = datetime.now(UTC) + timedelta(minutes=1)
    row = a.db.get(AuthSession, a.session_id)
    row.expires_at = at
    a.db.flush()

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return at

    monkeypatch.setattr(web_auth, "datetime", Clock)
    monkeypatch.setattr(deps, "datetime", Clock)
    _assert_unauthorized(a)


def test_subject_mismatch_is_rejected_before_live_principal_lookup(current_account, monkeypatch):
    a = current_account
    token, expires = issue_access_token(uuid4(), a.tid)
    a.db.add(AuthSession(tenant_id=a.tid, party_id=a.pid, token_hash=hash_token(token), expires_at=expires))
    a.db.flush()

    def forbidden(*args, **kwargs):
        raise AssertionError("mismatched signed subject reached principal lookup")

    monkeypatch.setattr(deps, "current_account", forbidden)
    with pytest.raises(HTTPException) as caught:
        _api(a, token)
    assert caught.value.status_code == 401


def test_roles_remain_tenant_local_even_with_live_account_from_other_tenant(current_account, tenant_b):
    a = current_account
    for guard in (web_auth.require_web_role("instructor"), deps.require_role("instructor")):
        with pytest.raises(HTTPException) as caught:
            guard(_request(tenant_b), _web(a), a.db)
        assert caught.value.status_code == 403


@pytest.mark.parametrize("email", [None, "", " "])
# This is the accepted canonical importer cohort, whose contract requires a
# normalized nonempty email. Raw legacy schema alone also permitted blanks.
def test_missing_account_email_refuses_without_default_or_legacy_person(current_account, email):
    a = current_account
    cached = a.db.get(Party, a.pid)
    a.admin.execute(update(Party).where(Party.tenant_id == a.tid, Party.id == a.pid).values(email=email))
    a.admin.commit()
    assert cached.email == a.email
    _assert_unauthorized(a)
