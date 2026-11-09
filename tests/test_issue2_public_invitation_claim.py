# ruff: noqa: F811  # imported pytest fixtures are redefined as test parameters
"""Issue #2 port of legacy web/test_canonical_public_invitation_claim.py.

Disposition: test_invitation_claim_spends_token_and_sibling_atomically
against app/services/invitation_claim.py via app/web/lifecycle routes.
All five legacy tests are ported; the PlatformSetting prior-value read
becomes settings_store.get_all over kernel domain_settings.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from dotmac_kernel.models import Tenant, UserCredential
from dotmac_kernel.security import verify_password
from sqlalchemy import select, text
from starlette.requests import Request

from app.services.settings_store import delete, get_all, set_many
from app.web import lifecycle as routes
from tests.issue2_wave1_fixtures import (  # noqa: F401 - pytest fixtures
    admin_engine,
    admin_session,
    app_client,
    app_user_session,
    canonical_account,
    current_account,
    invitation_account,
    lab_policy_rows,
    lab_worker_engine,
    lab_worker_session,
    no_real_console_spawn,
    policy,
    seed_lab,
    sign_in_account,
    tenant_a,
    tenant_b,
)

assert invitation_account


@pytest.fixture
def public_invitation(invitation_account):
    db, tid, party, profile, raw, token, sibling, _ = invitation_account
    tenant = db.get(Tenant, tid)
    assert tenant is not None
    token.expires_at = datetime.now(UTC) + timedelta(hours=1)
    db.flush()
    return db, tenant, party, profile, raw, token, sibling


def _scoped_request(tenant, *, htmx=False):
    headers = [(b"hx-request", b"true")] if htmx else []
    request = Request({"type": "http", "method": "POST", "path": "/accept-invite", "headers": headers})
    request.state.tenant = tenant
    return request


def test_invitation_claim_spends_token_and_sibling_atomically(public_invitation, monkeypatch):
    db, tenant, party, profile, raw, token, sibling = public_invitation
    root = db.get_transaction()

    # Invalid bearer: existing response shape, real proof unspent.
    for htmx, expected_status in ((False, 200), (True, 400)):
        response = routes.accept_submit(
            _scoped_request(tenant, htmx=htmx), token="invalid-<bearer>", password="welcome12", db=db
        )
        assert response.status_code == expected_status
        assert "Account was not activated" in response.body.decode()
        assert "invalid-<bearer>" not in response.body.decode()
        assert token.used_at is None and sibling.used_at is None
        assert db.get_transaction() is root and db.is_active

    # Unreadable policy propagates without consuming the proof.
    def unavailable(_db):
        raise RuntimeError("policy store unavailable")

    monkeypatch.setattr(routes, "invitation_acceptance_policy", unavailable)
    with pytest.raises(RuntimeError, match="policy store unavailable"):
        routes.accept_submit(_scoped_request(tenant), token=raw, password="welcome12", db=db)
    assert token.used_at is None and sibling.used_at is None
    assert db.is_active
    monkeypatch.undo()

    # Genuine claim: activation response, credential created, token and
    # sibling spent atomically, party/profile state untouched by claim.
    response = routes.accept_submit(_scoped_request(tenant), token=raw, password="welcome12", db=db)
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


def test_public_claim_htmx_success(public_invitation):
    db, tenant, _party, _, raw, token, sibling = public_invitation
    response = routes.accept_submit(_scoped_request(tenant, htmx=True), token=raw, password="welcome12", db=db)
    assert response.status_code == 200
    assert "Account activated" in response.body.decode()
    assert token.used_at is not None and sibling.used_at is not None


def test_existing_credential_conflict_keeps_proof_unspent(public_invitation):
    db, tenant, party, _, raw, token, sibling = public_invitation
    db.add(UserCredential(tenant_id=tenant.id, party_id=party.id, password_hash="existing-material"))
    db.flush()
    response = routes.accept_submit(_scoped_request(tenant, htmx=True), token=raw, password="welcome12", db=db)
    assert response.status_code == 400
    assert "Account was not activated" in response.body.decode()
    assert token.used_at is None and sibling.used_at is None
    assert db.is_active


def test_form_and_claim_use_the_same_actual_configured_minimum(public_invitation, admin_session):
    db, tenant, _, _, raw, token, sibling = public_invitation
    key = "invitation_password_min_length"
    assert admin_session.scalar(text("SELECT current_user")) in {"app_admin", "platform_api"}
    prior_value = get_all(admin_session).get(key)
    existed = prior_value is not None
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
