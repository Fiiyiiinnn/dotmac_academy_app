"""Authored hosted PostgreSQL owner/identity-map canaries; never run locally."""

import pytest
from dotmac_kernel.models import AuthSession
from dotmac_kernel.session_runtime import resolve_database_runtime

from app.services.credential_authentication import sign_in
from app.services.web_auth import revoke_session
from legacy_probe.services.test_canonical_current_user import current_account

assert current_account


@pytest.mark.parametrize("complete", ["commit", "rollback"])
def test_logout_actual_owner_revokes_only_selected_session_and_preserves_root(current_account, complete):
    a = current_account
    second = sign_in(a.db, tenant_id=a.tid, email=a.email, password="chosen-password9")
    assert second is not None
    a.db.commit()  # Fixture setup: both canonical sessions exist before logout.
    resolve_database_runtime().set_tenant(a.db, a.tid)
    selected = a.db.get(AuthSession, a.session_id)
    other = a.db.get(AuthSession, second.session_id)
    assert selected.revoked_at is None and other.revoked_at is None
    before_updated_at = selected.updated_at
    root = a.db.get_transaction()

    # Uses the actual public conflict SAVEPOINT and installed Core DML owner.
    revoke_session(a.db, a.tid, a.token)
    assert a.db.get_transaction() is root
    assert a.db.get(AuthSession, a.session_id) is selected
    assert selected.revoked_at is not None
    assert selected.updated_at == selected.revoked_at
    assert a.db.get(AuthSession, second.session_id).revoked_at is None
    revoked_at = selected.revoked_at

    if complete == "commit":
        a.db.commit()
    else:
        a.db.rollback()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    a.db.expire_all()
    persisted = a.db.get(AuthSession, a.session_id)
    if complete == "commit":
        assert persisted.revoked_at == persisted.updated_at == revoked_at
    else:
        assert persisted.revoked_at is None
        assert persisted.updated_at == before_updated_at
    assert a.db.get(AuthSession, second.session_id).revoked_at is None
