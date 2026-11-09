"""Actual two-flush absent storage canaries; authored NOT RUN.

Fixture writers are test setup, never authenticated bootstrap authority.
Existing web/invitation authorization and rollback canaries remain intact.
"""

import inspect
from dataclasses import replace
from uuid import uuid4

import pytest
from dotmac_kernel.models import UserCredential
from dotmac_kernel.transactions import conflict_savepoint
from sqlalchemy import select

from app.models.identity_extensions import AcademyCredentialPolicy
from app.services import credential_storage
from app.services.canonical_identity_store import create_person
from legacy_probe.services.test_canonical_current_user import current_account
from legacy_probe.services.test_canonical_identity_store import _values

assert current_account


@pytest.mark.parametrize('fail_flush', [None,1,2])
def test_shared_constructor_two_flushes_real_rows_and_rollback(current_account, monkeypatch, fail_flush):
    a = current_account
    values = replace(_values(a.tid,uuid4().hex+'@example.test'),profile_status='custom_review')
    create_person(a.db,values=values)
    cid = uuid4()
    root = a.db.get_transaction()
    calls = []
    flush = a.db.flush

    def observed(*args, **kwargs):
        frame = inspect.currentframe().f_back
        selected = frame.f_code.co_name == 'create_absent' and frame.f_globals['__name__'] == credential_storage.__name__
        result = flush(*args, **kwargs)
        if selected:
            calls.append(True)
            if len(calls) == fail_flush:
                raise RuntimeError('after actual shared storage flush')
        return result

    # Reuse a genuine credential entirely within the test fixture. This private
    # constructor does not hash, normalize, authorize, commit or own a savepoint.
    fixture_hash = a.db.scalar(select(UserCredential.password_hash).where(UserCredential.id == a.credential_id))
    monkeypatch.setattr(a.db,'flush',observed)

    def write():
        return credential_storage.create_absent(
            a.db,credential_id=cid,tenant_id=a.tid,party_id=values.id,email=values.email,
            password_hash=fixture_hash,occurred_at=values.created_at,
        )

    if fail_flush is not None:
        with pytest.raises(RuntimeError,match='actual shared storage flush'), conflict_savepoint(a.db):
            write()
        assert len(calls) == fail_flush
        assert a.db.get(UserCredential,cid) is None
        assert a.db.get(AcademyCredentialPolicy,cid) is None
    else:
        credential,policy = write()
        assert len(calls) == 2
        assert credential.id == cid and credential.party_id == values.id and credential.tenant_id == a.tid
        assert credential.password_hash == fixture_hash
        assert credential.created_at == credential.updated_at == values.created_at
        assert policy.credential_id == cid and policy.tenant_id == a.tid and policy.email == values.email
        assert (policy.lifecycle_version,policy.failed_login_attempts,policy.reset_required) == (0,0,False)
        assert policy.locked_until is None
        assert policy.created_at == policy.updated_at == values.created_at
    assert a.db.get_transaction() is root
