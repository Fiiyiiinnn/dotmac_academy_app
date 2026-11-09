"""Hosted PostgreSQL canaries, authored NOT RUN; real canonical fixture lineage.

No mock Session/SQLite/constructed authority. The existing current_account
fixture requires actual hosted composition, canonical installation and app_user.
Storage facts below establish no invitation or account-creation authority.
"""

import inspect
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from threading import Event
from uuid import uuid4

import pytest
from dotmac_kernel.models import Party, PartyPerson, PartyRoleGrant, Role
from dotmac_kernel.session_runtime import resolve_database_runtime
from dotmac_kernel.transactions import conflict_savepoint
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.models.identity_extensions import AcademyAccountProfile, AcademyCredentialPolicy
from app.services import canonical_identity_store as store
from legacy_probe.services.test_canonical_current_user import current_account
from legacy_probe.services.test_canonical_self_profile_locking import _wait_for_lock

assert current_account
NOW = datetime(2026, 10, 18, tzinfo=UTC)


def _values(tenant_id, email):
    return store.NewPersonValues(
        id=uuid4(),
        tenant_id=tenant_id,
        email=email,
        display_name=" Exact caller display ",
        first_name=" First ",
        last_name=" Last ",
        is_active=False,
        custom_fields={"custom": ["kept"]},
        profile_status="suspended",
        profile_avatar_path="avatar-path",
        profile_prefs={"nested": [1]},
        created_at=NOW,
        updated_at=NOW,
    )


def test_explicit_creation_three_flushes_detached_facts_and_caller_boundary(current_account, monkeypatch):
    a = current_account
    root, nested = a.db.get_transaction(), a.db.get_nested_transaction()
    values = _values(a.tid, f"MiXeD-{uuid4().hex}@Example.Test")
    callers = []
    original = a.db.flush

    def spy(*args, **kwargs):
        callers.append(inspect.currentframe().f_back.f_code.co_name)
        return original(*args, **kwargs)

    monkeypatch.setattr(a.db, "flush", spy)
    fact = store.create_person(a.db, values=values)
    assert callers.count("create_person") == 3
    assert a.db.get_transaction() is root and a.db.get_nested_transaction() is nested
    assert fact == store.PartyFact(values.id, a.tid, fact.party_type, values.email, values.display_name, False)
    with pytest.raises(FrozenInstanceError):
        fact.email = "changed"
    values.custom_fields["custom"].append("caller mutation")
    values.profile_prefs["nested"].append(2)
    party = a.db.get(Party, values.id)
    assert party.custom_fields == {"custom": ["kept"]}
    assert (party.created_at, party.updated_at) == (NOW, NOW)
    assert store.lock_person(a.db, tenant_id=a.tid, party_id=values.id) == store.PersonFact(
        values.id, " First ", " Last "
    )
    profile = store.lock_profile(a.db, tenant_id=a.tid, party_id=values.id)
    assert (profile.status, profile.avatar_path, profile.prefs) == ("suspended", "avatar-path", {"nested": [1]})
    profile.prefs["nested"].append(3)
    assert a.db.get(AcademyAccountProfile, values.id).prefs == {"nested": [1]}
    assert store.credential_member_ids(a.db, tenant_id=a.tid, party_id=values.id) == ()


def test_exact_lookup_never_falls_back_to_normalized_reuse(current_account):
    a = current_account
    email = f" MiXeD-{uuid4().hex}@Example.Test "
    created = store.create_person(a.db, values=_values(a.tid, email))
    assert store.find_party_exact_email(a.db, tenant_id=a.tid, email=email) == created
    assert store.find_party_exact_email(a.db, tenant_id=a.tid, email=email.strip().lower()) is None
    assert store.find_party_normalized_email(a.db, tenant_id=a.tid, normalized_email=email.strip().lower()) == created
    assert store.find_party_normalized_email(a.db, tenant_id=a.tid, normalized_email=email) is None


def test_exact_miss_uniqueness_failure_propagates_without_fuzzy_reuse(current_account):
    a = current_account
    email = f"Mixed-{uuid4().hex}@Example.Test"
    created = store.create_person(a.db, values=_values(a.tid, email))
    assert store.find_party_exact_email(a.db, tenant_id=a.tid, email=email.lower()) is None
    with pytest.raises(IntegrityError), conflict_savepoint(a.db):
        store.create_person(a.db, values=_values(a.tid, email.lower()))
    assert store.find_party_exact_email(a.db, tenant_id=a.tid, email=email) == created


@pytest.mark.parametrize("step", [1, 2, 3])
def test_caller_savepoint_rolls_back_each_creation_flush_failure(current_account, monkeypatch, step):
    a = current_account
    values = _values(a.tid, f"rollback-{uuid4().hex}@example.test")
    root = a.db.get_transaction()
    original = a.db.flush
    count = 0

    def fail(*args, **kwargs):
        nonlocal count
        from_create = inspect.currentframe().f_back.f_code.co_name == "create_person"
        result = original(*args, **kwargs)
        if from_create:
            count += 1
            if count == step:
                raise RuntimeError("injected after real flush")
        return result

    monkeypatch.setattr(a.db, "flush", fail)
    with pytest.raises(RuntimeError, match="injected"), conflict_savepoint(a.db):
        store.create_person(a.db, values=values)
    assert a.db.get_transaction() is root and a.db.get_nested_transaction() is None
    assert a.db.get(Party, values.id) is None
    assert a.db.get(PartyPerson, values.id) is None
    assert a.db.get(AcademyAccountProfile, values.id) is None
    assert a.db.scalar(text("SELECT current_setting('app.current_tenant')")) == str(a.tid)


def test_all_secondary_reads_and_membership_are_tenant_scoped(current_account):
    a = current_account
    wrong = uuid4()
    store.lock_parties(a.db, tenant_id=a.tid, party_ids=(a.pid,))
    assert store.credential_member_ids(a.db, tenant_id=a.tid, party_id=a.pid) == (a.credential_id,)
    assert store.find_party_exact_email(a.db, tenant_id=wrong, email=a.email) is None
    assert store.find_party_normalized_email(a.db, tenant_id=wrong, normalized_email=a.email) is None
    assert store.lock_parties(a.db, tenant_id=wrong, party_ids=(a.pid,)) == ()
    assert store.lock_person(a.db, tenant_id=wrong, party_id=a.pid) is None
    assert store.lock_profile(a.db, tenant_id=wrong, party_id=a.pid) is None
    assert store.lock_roles(a.db, tenant_id=wrong) == ()
    assert store.lock_grants(a.db, tenant_id=wrong, party_ids=(a.pid,)) == ()
    assert store.credential_member_ids(a.db, tenant_id=wrong, party_id=a.pid) == ()


def test_any_credential_alias_suppresses_absence_without_reading_material(current_account):
    a = current_account
    # Genuine sign-in fixture: move its product alias away from Party.email.
    # No credential is manufactured or changed by the fact store.
    a.db.commit()
    policy = a.admin.get(AcademyCredentialPolicy, a.credential_id)
    alias = f"different-alias-{uuid4().hex}@example.test"
    policy.email = alias
    a.admin.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert alias != a.email
    store.lock_parties(a.db, tenant_id=a.tid, party_ids=(a.pid,))
    profile = a.db.get(AcademyAccountProfile, a.pid)
    before = (profile.status, profile.avatar_path, profile.prefs)
    ids = store.credential_member_ids(a.db, tenant_id=a.tid, party_id=a.pid)
    assert ids == (a.credential_id,) and all(type(value) is type(a.credential_id) for value in ids)
    assert bool(ids)  # No alias, enabled-status or account-profile filter.
    assert (profile.status, profile.avatar_path, profile.prefs) == before


def test_additive_roles_grants_preserve_custom_names_and_noop_flush(current_account, monkeypatch):
    a = current_account
    store.lock_parties(a.db, tenant_id=a.tid, party_ids=(a.pid,))
    catalog = store.lock_roles(a.db, tenant_id=a.tid)
    slug = f"custom-{uuid4().hex}"
    roles = store.provision_missing_roles(
        a.db,
        tenant_id=a.tid,
        catalog=catalog,
        configured_names={slug: " Exact custom name "},
        missing_role_ids={slug: uuid4()},
        created_at=NOW,
        updated_at=NOW,
    )
    role = next(row for row in roles if row.slug == slug)
    before = store.lock_grants(a.db, tenant_id=a.tid, party_ids=(a.pid,))
    grant = store.ensure_grant(
        a.db, tenant_id=a.tid, party_id=a.pid, role_id=role.id, new_grant_id=uuid4(), created_at=NOW, updated_at=NOW
    )
    flushes = []
    original = a.db.flush

    def spy(*args, **kwargs):
        flushes.append(inspect.currentframe().f_back.f_code.co_name)
        return original(*args, **kwargs)

    monkeypatch.setattr(a.db, "flush", spy)
    repeated = store.provision_missing_roles(
        a.db,
        tenant_id=a.tid,
        catalog=store.lock_roles(a.db, tenant_id=a.tid),
        configured_names={slug: "do not rename"},
        missing_role_ids={},
        created_at=NOW,
        updated_at=NOW,
    )
    assert next(row for row in repeated if row.slug == slug).name == " Exact custom name "
    assert (
        store.ensure_grant(
            a.db, tenant_id=a.tid, party_id=a.pid, role_id=role.id, new_grant_id=uuid4(), created_at=NOW, updated_at=NOW
        )
        == grant
    )
    assert {row.id for row in store.lock_grants(a.db, tenant_id=a.tid, party_ids=(a.pid,))} == {
        grant.id,
        *(row.id for row in before),
    }
    assert "provision_missing_roles" not in flushes and "ensure_grant" not in flushes
    assert a.db.get(Role, role.id).name == " Exact custom name "
    assert a.db.get(PartyRoleGrant, grant.id).created_at == NOW


def test_reverse_input_party_locks_wait_in_shared_uuid_order(current_account):
    a = current_account
    # Seed genuine canonical target, commit setup before independent roots.
    target = store.create_person(a.db, values=_values(a.tid, f"locks-{uuid4().hex}@example.test"))
    a.db.commit()
    factory = sessionmaker(bind=a.db.get_bind(), autoflush=False)
    locked, started, release = Event(), Event(), Event()
    backend = []
    expected = sorted((a.pid, target.id))

    def leader():
        with factory() as db:
            resolve_database_runtime().set_tenant(db, a.tid)
            facts = store.lock_parties(db, tenant_id=a.tid, party_ids=(target.id, a.pid, a.pid))
            assert [fact.id for fact in facts] == expected
            locked.set()
            assert release.wait(10)
            db.rollback()

    def follower():
        with factory() as db:
            resolve_database_runtime().set_tenant(db, a.tid)
            backend.append(db.scalar(text("SELECT pg_backend_pid()")))
            started.set()
            facts = store.lock_parties(db, tenant_id=a.tid, party_ids=(a.pid, target.id))
            assert [fact.id for fact in facts] == expected
            db.rollback()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(leader)
        assert locked.wait(10)
        second = pool.submit(follower)
        assert started.wait(10)
        try:
            resolve_database_runtime().set_tenant(a.db, a.tid)
            _wait_for_lock(a.db, backend[0])
        finally:
            release.set()
        first.result(timeout=20)
        second.result(timeout=20)


@pytest.mark.parametrize("operation", ["create", "grant"])
def test_non_bypass_foreign_tenant_writes_refuse_in_caller_savepoint(current_account, tenant_b, operation):
    from sqlalchemy.exc import DBAPIError

    a = current_account
    assert a.tid != tenant_b.id
    assert not a.db.execute(
        text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user")
    ).scalar_one()
    store.lock_parties(a.db, tenant_id=a.tid, party_ids=(a.pid,))
    root = a.db.get_transaction()
    values = _values(tenant_b.id, f"foreign-{uuid4().hex}@example.test")
    with pytest.raises(DBAPIError), conflict_savepoint(a.db):
        if operation == "create":
            store.create_person(a.db, values=values)
        else:
            store.ensure_grant(
                a.db,
                tenant_id=tenant_b.id,
                party_id=a.pid,
                role_id=uuid4(),
                new_grant_id=uuid4(),
                created_at=NOW,
                updated_at=NOW,
            )
    assert a.db.get_transaction() is root and a.db.get_nested_transaction() is None
    assert a.db.get(Party, values.id) is None
    assert a.db.scalar(text("SELECT current_setting('app.current_tenant')")) == str(a.tid)
