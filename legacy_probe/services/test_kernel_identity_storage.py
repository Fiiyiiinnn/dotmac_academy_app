"""Hosted canonical wrapper split canaries, authored NOT RUN.

Reuse genuine current_account and migrated product lineage. Existing store
canaries remain byte-exact and also exercise the public generic aliases.
"""

import inspect
from dataclasses import replace
from uuid import uuid4

import pytest
from dotmac_kernel import identity_storage as kernel_store
from dotmac_kernel.models import Party, PartyPerson
from dotmac_kernel.transactions import conflict_savepoint

from app.models.identity_extensions import AcademyAccountProfile
from app.services import canonical_identity_store as store
from legacy_probe.services.test_canonical_current_user import current_account
from legacy_probe.services.test_canonical_identity_store import _values

assert current_account


def test_product_status_and_profile_third_flush_preserved(current_account, monkeypatch):
    a = current_account
    values = replace(_values(a.tid, uuid4().hex + "@example.test"), profile_status="custom_review")
    root = a.db.get_transaction()
    calls = []
    original = a.db.flush

    def flush(*args, **kwargs):
        caller = inspect.currentframe().f_back
        if caller.f_code.co_name == "create_person":
            calls.append(caller.f_globals["__name__"])
        return original(*args, **kwargs)

    monkeypatch.setattr(a.db, "flush", flush)
    fact = store.create_person(a.db, values=values)
    assert calls == [kernel_store.__name__, kernel_store.__name__, store.__name__]
    assert a.db.get_transaction() is root
    assert store.lock_parties is kernel_store.lock_parties
    assert store.PartyFact is kernel_store.PartyFact
    profile = store.lock_profile(a.db, tenant_id=a.tid, party_id=fact.id)
    assert profile.status == "custom_review"
    assert (profile.avatar_path, profile.prefs) == (values.profile_avatar_path, {"nested": [1]})
    values.profile_prefs["nested"].append(2)
    profile.prefs["nested"].append(3)
    assert a.db.get(AcademyAccountProfile, fact.id).prefs == {"nested": [1]}
    assert store.lock_profile(a.db, tenant_id=uuid4(), party_id=fact.id) is None


def test_profile_flush_failure_rolls_back_canonical_rows_in_same_root(current_account, monkeypatch):
    a = current_account
    root = a.db.get_transaction()
    values = _values(a.tid, uuid4().hex + "@example.test")
    original = a.db.flush

    def flush(*args, **kwargs):
        caller = inspect.currentframe().f_back
        result = original(*args, **kwargs)
        if caller.f_code.co_name == "create_person" and caller.f_globals["__name__"] == store.__name__:
            raise RuntimeError("after actual profile flush")
        return result

    monkeypatch.setattr(a.db, "flush", flush)
    with pytest.raises(RuntimeError, match="actual profile flush"), conflict_savepoint(a.db):
        store.create_person(a.db, values=values)
    assert a.db.get_transaction() is root
    assert a.db.get_nested_transaction() is None
    assert a.db.get(Party, values.id) is None
    assert a.db.get(PartyPerson, values.id) is None
    assert a.db.get(AcademyAccountProfile, values.id) is None


def test_profile_flush_listener_change_is_reflected_in_final_fact(current_account):
    from sqlalchemy import event

    a = current_account
    values = _values(a.tid, uuid4().hex + "@example.test")
    seen = []

    def before_flush(db, flush_context, instances):
        if any(isinstance(row, AcademyAccountProfile) and row.party_id == values.id for row in db.new):
            party = next(
                (row for row in db.identity_map.values() if isinstance(row, Party) and row.id == values.id), None
            )
            assert party is not None  # Wrapper holds the actual strong row reference.
            party.display_name = "Profile flush observation"
            seen.append(party)

    event.listen(a.db, "before_flush", before_flush)
    try:
        fact = store.create_person(a.db, values=values)
    finally:
        event.remove(a.db, "before_flush", before_flush)
    assert len(seen) == 1
    assert fact.display_name == "Profile flush observation"
    assert a.db.get(Party, values.id) is seen[0]
    assert a.db.get(Party, values.id).display_name == fact.display_name
