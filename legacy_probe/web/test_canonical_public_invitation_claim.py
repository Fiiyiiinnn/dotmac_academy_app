"""Authored hosted route-to-owner canaries; no local runtime qualification.

These use the genuine migrated app_user invitation fixture and the actual
configured claim owner. Calling the route directly proves its delegation and
response contract; full TestClient/CSRF and original lifecycle fixture adoption
remain separate gates. No token, credential or authority shim is installed.
"""

from datetime import UTC, datetime, timedelta

import pytest
from dotmac_kernel.models import UserCredential
from dotmac_kernel.security import verify_password
from sqlalchemy import select, text
from starlette.requests import Request

from app.models.platform_settings import PlatformSetting
from app.services.settings_store import delete, set_many
from app.web import lifecycle as routes
from legacy_probe.services.test_invitation_claim import invitation_account

assert invitation_account


@pytest.fixture
def public_invitation(invitation_account):
    db, tid, party, profile, raw, token, sibling, _ = invitation_account
    from dotmac_kernel.models import Tenant

    tenant = db.get(Tenant, tid)
    assert tenant is not None
    # The service fixture's equality-expiry fact uses an injected clock. A real
    # public call samples the current clock, so give it a genuinely live proof.
    token.expires_at = datetime.now(UTC) + timedelta(hours=1)
    db.flush()
    return db, tenant, party, profile, raw, token, sibling


def _scoped_request(tenant, *, htmx=False):
    headers = [(b"hx-request", b"true")] if htmx else []
    request = Request({"type": "http", "method": "POST", "path": "/accept-invite", "headers": headers})
    # Routing scope is not claim authority: the owner independently proves
    # the actual scoped bearer and target account before creating a credential.
    request.state.tenant = tenant
    return request


@pytest.mark.parametrize("htmx", [False, True])
def test_public_claim_preserves_activation_response_and_real_owner_effects(public_invitation, htmx):
    db, tenant, party, profile, raw, token, sibling = public_invitation
    root = db.get_transaction()
    response = routes.accept_submit(_scoped_request(tenant, htmx=htmx), token=raw, password="welcome12", db=db)
    assert response.status_code == 200
    assert "Account activated" in response.body.decode()
    assert "Your password has been saved. You can now sign in." in response.body.decode()
    assert raw not in response.body.decode()
    credential = db.scalars(
        select(UserCredential).where(UserCredential.tenant_id == tenant.id, UserCredential.party_id == party.id)
    ).one()
    assert verify_password("welcome12", credential.password_hash)
    assert token.used_at is not None and sibling.used_at is not None
    assert party.is_active is False and profile.status == "suspended"
    assert db.get_transaction() is root and db.is_active
    assert db.scalar(text("SELECT current_setting('app.current_tenant')")) == str(tenant.id)


@pytest.mark.parametrize("htmx,expected_status", [(False, 200), (True, 400)])
def test_invalid_bearer_keeps_existing_response_shape_without_spending_real_proof(
    public_invitation, htmx, expected_status
):
    db, tenant, _, _, _, token, sibling = public_invitation
    root = db.get_transaction()
    response = routes.accept_submit(
        _scoped_request(tenant, htmx=htmx), token="invalid-<bearer>", password="welcome12", db=db
    )
    assert response.status_code == expected_status
    assert "Account was not activated" in response.body.decode()
    assert "invalid-<bearer>" not in response.body.decode()
    assert token.used_at is None and sibling.used_at is None
    assert db.get_transaction() is root and db.is_active


def test_form_and_claim_use_the_same_actual_configured_minimum(public_invitation, admin_session):
    db, tenant, _, _, raw, token, sibling = public_invitation
    key = "invitation_password_min_length"
    # Settings are globally readable but writable only by their real owner or
    # settings-writer role. Publish setup through the authorized admin session;
    # the route and claim retain their restricted app_user connection/root.
    assert admin_session.scalar(text("SELECT current_user")) in {"app_admin", "platform_api"}
    prior = admin_session.get(PlatformSetting, key)
    existed, prior_value = prior is not None, prior.value if prior is not None else None
    # Refuse malformed prior store state before any committed setup mutation,
    # ensuring the strict restoration path is valid for the captured value.
    routes.invitation_acceptance_policy(admin_session)
    set_many(admin_session, {key: "11"})
    admin_session.commit()
    try:
        response = routes.accept_form(_scoped_request(tenant), token='<unsafe>&"', db=db)
        body = response.body.decode()
        assert "minlength='11'" in body
        assert "Choose a password (min 11 chars)" in body
        assert "&lt;unsafe&gt;&amp;&quot;" in body
        assert "<unsafe>" not in body
        response = routes.accept_submit(_scoped_request(tenant, htmx=True), token=raw, password="welcome12", db=db)
        assert response.status_code == 400
        assert "Account was not activated" in response.body.decode()
        assert token.used_at is None and sibling.used_at is None
        response = routes.accept_submit(_scoped_request(tenant, htmx=True), token=raw, password="welcome12345", db=db)
        assert response.status_code == 200
        assert token.used_at is not None and sibling.used_at is not None
    finally:
        admin_session.rollback()
        if existed:
            set_many(admin_session, {key: prior_value})
        else:
            delete(admin_session, key)
        admin_session.commit()


def test_existing_credential_conflict_keeps_proof_unspent(public_invitation):
    db, tenant, party, _, raw, token, sibling = public_invitation
    db.add(UserCredential(tenant_id=tenant.id, party_id=party.id, password_hash="existing-material"))
    db.flush()
    response = routes.accept_submit(_scoped_request(tenant, htmx=True), token=raw, password="welcome12", db=db)
    assert response.status_code == 400
    assert "Account was not activated" in response.body.decode()
    assert token.used_at is None and sibling.used_at is None
    assert db.is_active


def test_unreadable_policy_propagates_without_consuming_proof(public_invitation, monkeypatch):
    db, tenant, _, _, raw, token, sibling = public_invitation

    def unavailable(_db):
        raise RuntimeError("policy store unavailable")

    monkeypatch.setattr(routes, "invitation_acceptance_policy", unavailable)
    with pytest.raises(RuntimeError, match="policy store unavailable"):
        routes.accept_submit(_scoped_request(tenant), token=raw, password="welcome12", db=db)
    assert token.used_at is None and sibling.used_at is None
    assert db.is_active
