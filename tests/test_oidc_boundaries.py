"""OIDC persistence boundaries against genesis PostgreSQL; hosted CI only."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from dotmac_auth_oidc import LoginState
from dotmac_kernel.models import AuthSession, Party
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.services import oidc_login, web_auth
from app.services.security import hash_token
from tests.issue2_wave3_support import current_account, grant_std
from tests.test_oidc_login import SUBJECT, _FakeClient

assert current_account  # Imported pytest fixture.


def _state():
    return LoginState(
        state_id=uuid4().hex,
        nonce="independent-nonce",
        code_verifier="independent-verifier",
        redirect_uri="https://academy.example.test/login/oidc/callback",
        issued_at=1700000000,
        return_to="/account",
    )


def _scope(db, tenant_id):
    db.execute(text("SELECT set_config('app.current_tenant', :tenant, true)"), {"tenant": str(tenant_id)})


def _store(db, tenant_id):
    return oidc_login.AcademyOidcStateStore(db, tenant_id=tenant_id, provider_binding=oidc_login.provider_binding())


def test_disabling_staff_oidc_revokes_only_federated_sessions(current_account):
    a = current_account
    grant_std(a.admin, tenant_id=a.tid, party_id=a.pid, role="admin")
    a.admin.commit()
    party = a.db.get(Party, a.pid)
    binding = oidc_login.bind_staff_identity(
        a.db, tenant=a.tenant, party=party, subject=SUBJECT, bound_by="test-operator", reason="selective disable proof"
    )
    binding_id = binding.id
    a.db.commit()
    protocol = _FakeClient(SUBJECT)
    started = oidc_login.begin_login(a.db, tenant=a.tenant, client=protocol)
    a.db.commit()
    completed = oidc_login.complete_login(
        a.db, tenant=a.tenant, code="code", state=started.state, stored_state=started.state, client=protocol
    )
    a.db.commit()
    assert web_auth._current_person(a.db, a.tid, completed.session_token) is not None
    assert web_auth._current_person(a.db, a.tid, a.token) is not None
    a.db.commit()

    # A genuine caller-owned root transaction is required by the kernel owner.
    with a.db.begin():
        assert oidc_login.disable_staff_identities(a.db, tenant=a.tenant, party_id=a.pid) == 1
    sessions = list(a.db.scalars(select(AuthSession).where(AuthSession.party_id == a.pid)))
    federated = [row for row in sessions if row.external_identity_binding_id == binding_id]
    password = [row for row in sessions if row.token_hash == hash_token(a.token)]
    assert len(federated) == len(password) == 1
    assert federated[0].revoked_at is not None
    assert password[0].external_identity_binding_id is None and password[0].revoked_at is None
    assert web_auth._current_person(a.db, a.tid, completed.session_token) is None
    assert web_auth._current_person(a.db, a.tid, a.token) is not None
    # Fresh password login remains possible after disabling the external binding.
    assert web_auth.sign_in(a.db, tenant_id=a.tid, email=a.email, password="chosen-password9") is not None
    a.db.commit()
    started = oidc_login.begin_login(a.db, tenant=a.tenant, client=protocol)
    a.db.commit()
    with pytest.raises(oidc_login.OidcLoginRefused):
        oidc_login.complete_login(
            a.db, tenant=a.tenant, code="code", state=started.state, stored_state=started.state, client=protocol
        )
    a.db.rollback()


def test_ceremony_expiry_and_tenant_rls_preserve_other_tenants(current_account):
    a = current_account
    live, expired = _state(), _state()
    _store(a.db, a.tid).put(live, ttl_seconds=600)
    _store(a.db, a.tid).put(expired, ttl_seconds=-1)
    a.db.commit()
    assert _store(a.db, a.tid).take(expired.state_id) is None
    a.db.commit()
    with Session(a.app_engine) as other, other.begin():
        _scope(other, a.tenant_b.id)
        # No tenant predicate: visibility itself must be constrained by RLS.
        assert other.execute(text("SELECT state_hash FROM public.academy_oidc_login_states")).all() == []
        # Even a caller naming tenant A cannot consume its state from tenant B.
        assert _store(other, a.tid).take(live.state_id) is None
    with Session(a.app_engine) as other, pytest.raises(DBAPIError) as denied, other.begin():
        _scope(other, a.tenant_b.id)
        _store(other, a.tid).put(_state(), ttl_seconds=600)
    assert denied.value.orig.sqlstate == "42501"  # PostgreSQL insufficient_privilege / RLS WITH CHECK.
    assert _store(a.db, a.tid).take(live.state_id) == live
    a.db.commit()


def test_concurrent_ceremony_claim_has_one_winner_with_intact_metadata(current_account):
    a = current_account
    state = _state()
    _store(a.db, a.tid).put(state, ttl_seconds=600)
    a.db.commit()
    ready = Barrier(2)

    def claim():
        with Session(a.app_engine) as db, db.begin():
            _scope(db, a.tid)
            db.execute(text("SET LOCAL statement_timeout = '10s'"))
            ready.wait(timeout=10)
            return _store(db, a.tid).take(state.state_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim) for _ in range(2)]
        claims = [future.result(timeout=30) for future in futures]
    assert sum(claimed is None for claimed in claims) == 1
    assert [claimed for claimed in claims if claimed is not None] == [state]
    assert _store(a.db, a.tid).take(state.state_id) is None
    a.db.commit()


def test_wrong_provider_cannot_destroy_another_providers_ceremony(current_account):
    a = current_account
    state = _state()
    _store(a.db, a.tid).put(state, ttl_seconds=600)
    a.db.commit()
    wrong_provider = oidc_login.AcademyOidcStateStore(a.db, tenant_id=a.tid, provider_binding="another-provider")
    assert wrong_provider.take(state.state_id) is None
    a.db.commit()
    # A refusal must not destroy the ceremony belonging to the correct provider.
    assert _store(a.db, a.tid).take(state.state_id) == state
    a.db.commit()
