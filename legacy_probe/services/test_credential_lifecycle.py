"""Hosted-only canonical identity reset canaries.

Requires the composed canonical identity schema, Academy policy reset_required,
retargeted AccountToken FKs and installed assembly audit declarations. These
tests intentionally do not create a substitute schema or install fake ports.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from dotmac_kernel.audit import AuditEvent
from dotmac_kernel.models import AuthSession, Party, PartyPerson, PartyType, UserCredential
from dotmac_kernel.security import verify_password
from sqlalchemy import select, update
from starlette.requests import Request

from app.models.account_token import AccountToken
from app.models.identity_extensions import AcademyCredentialPolicy
from app.services import credential_lifecycle as adapter
from app.services.credential_policy import AcademyPasswordPolicy
from app.services.exceptions import BadRequestError
from app.services.security import hash_token

NOW = datetime(2026, 10, 18, 12, tzinfo=UTC)


@pytest.fixture
def reset_account(admin_session, tenant_a):
    db = admin_session
    assert db.get_bind().dialect.name == "postgresql"
    party = Party(
        tenant_id=tenant_a.id,
        party_type=PartyType.person,
        display_name="Reset",
        email=f"Reset-{uuid4().hex}@a.edu",
        is_active=False,
    )
    db.add(party)
    db.flush()
    db.add(PartyPerson(party_id=party.id, first_name="Reset", last_name="Subject"))
    db.flush()
    credentials = []
    for version in (2, 7):
        credential = UserCredential(tenant_id=tenant_a.id, party_id=party.id, password_hash="old-hash")
        db.add(credential)
        db.flush()
        db.add(
            AcademyCredentialPolicy(
                credential_id=credential.id,
                tenant_id=tenant_a.id,
                email=party.email if version == 2 else f"alias-{uuid4().hex}@a.edu",
                failed_login_attempts=4,
                locked_until=NOW + timedelta(hours=1),
                lifecycle_version=version,
                reset_required=True,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        credentials.append(credential)
    raw = uuid4().hex + uuid4().hex
    token = AccountToken(
        tenant_id=tenant_a.id,
        person_id=party.id,
        kind="password_reset",
        token_hash=hash_token(raw),
        expires_at=NOW + timedelta(hours=2),
    )
    sibling = AccountToken(
        tenant_id=tenant_a.id,
        person_id=party.id,
        kind="password_reset",
        token_hash=hash_token(uuid4().hex),
        expires_at=NOW - timedelta(hours=1),
    )
    sessions = [
        AuthSession(
            tenant_id=tenant_a.id,
            party_id=party.id,
            token_hash=hash_token(uuid4().hex),
            expires_at=expiry,
            revoked_at=stamp,
        )
        for expiry, stamp in (
            (NOW + timedelta(hours=1), None),
            (NOW - timedelta(hours=1), None),
            (NOW - timedelta(hours=1), NOW - timedelta(days=1)),
        )
    ]
    db.add_all([token, sibling, *sessions])
    db.flush()
    return db, tenant_a.id, party, credentials, raw, token, sibling, sessions


def _reset(account, **overrides):
    db, tid, _, _, raw, *_ = account
    return adapter.complete_password_reset(
        db,
        tenant_id=tid,
        raw=overrides.get("raw", raw),
        new_password=overrides.get("password", "chosen-new9"),
        password_policy=overrides.get("password_policy", AcademyPasswordPolicy(minimum_length=8)),
        now=NOW,
    )


def test_reset_one_hash_all_versions_aliases_sessions_and_current_audit(reset_account, monkeypatch):
    import dotmac_kernel.credential_lifecycle as owner

    db, tid, party, credentials, _, token, sibling, sessions = reset_account
    calls = []
    original = owner.hash_password

    def hash_once(secret):
        calls.append(secret)
        return original(secret)

    monkeypatch.setattr(owner, "hash_password", hash_once)
    assert _reset(reset_account).id == party.id
    assert len(calls) == 1
    assert len({row.password_hash for row in credentials}) == 1
    assert verify_password("chosen-new9", credentials[0].password_hash)
    policies = list(db.scalars(select(AcademyCredentialPolicy).where(AcademyCredentialPolicy.tenant_id == tid)))
    assert sorted(row.lifecycle_version for row in policies) == [3, 8]
    assert all(
        not row.reset_required and row.failed_login_attempts == 0 and row.locked_until is None for row in policies
    )
    assert party.is_active is False
    assert party.email == party.email.strip().lower()
    assert sum(row.email == party.email for row in policies) == 1
    assert token.used_at == sibling.used_at == NOW
    assert [row.revoked_at for row in sessions] == [NOW, NOW, NOW - timedelta(days=1)]
    events = list(db.scalars(select(AuditEvent).where(AuditEvent.tenant_id == tid)))
    assert sorted(row.action for row in events) == [
        "credential.reset.completed",
        "credential.reset.completed",
        "credential.sessions.revoked",
    ]
    assert all(row.actor_type == "user" and row.actor_id == str(party.id) for row in events)
    assert all(
        "chosen-new9" not in repr(row.details) and credentials[0].password_hash not in repr(row.details)
        for row in events
    )


@pytest.mark.parametrize("change", ["used", "expired", "kind", "tampered", "tenant", "principal"])
def test_real_hmac_and_refreshed_token_binding_refuse(reset_account, change):
    db, _, _, credentials, _, token, _, _ = reset_account
    if change == "used":
        token.used_at = NOW
    elif change == "expired":
        token.expires_at = NOW - timedelta(seconds=1)
    elif change == "kind":
        token.kind = "invite"
    elif change == "tenant":
        # Wrong tenant is refused without changing an FK-bound row.
        with pytest.raises(BadRequestError):
            adapter.complete_password_reset(
                db,
                tenant_id=uuid4(),
                raw=reset_account[4],
                new_password="chosen-new9",
                password_policy=AcademyPasswordPolicy(minimum_length=8),
                now=NOW,
            )
        return
    elif change == "principal":
        # A valid HMAC is insufficient without a matching canonical person.
        db.delete(db.get(PartyPerson, reset_account[2].id))
    db.flush()
    with pytest.raises(BadRequestError):
        _reset(reset_account, raw=reset_account[4] + "x" if change == "tampered" else reset_account[4])
    assert all(row.password_hash == "old-hash" for row in credentials)


def test_password_minimum_precedes_any_token_work(reset_account, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("token work reached")

    monkeypatch.setattr(adapter, "_candidate", forbidden)
    with pytest.raises(BadRequestError, match="configured account policy"):
        _reset(reset_account, password="short")


def test_replay_refused(reset_account):
    _reset(reset_account)
    with pytest.raises(BadRequestError):
        _reset(reset_account)


@pytest.mark.parametrize(
    "step",
    [
        "consume",
        "lock_current_credentials",
        "write_hash",
        "write_hash_second",
        "clear_failure_state",
        "revoke_sessions",
        "_audit",
        "record_event",
        "record_event_second",
        "invalidate_siblings",
    ],
)
def test_caught_failure_rolls_back_proof_and_all_effects_before_outer_commit(reset_account, monkeypatch, step):
    db, tid, _, credentials, _, token, sibling, sessions = reset_account
    method = step.removesuffix("_second")
    original = getattr(adapter._ResetPorts, method)
    calls = 0

    def fail_after(self, *args, **kwargs):
        nonlocal calls
        result = original(self, *args, **kwargs)
        calls += 1
        if calls == (2 if step.endswith("_second") else 1):
            raise adapter.CredentialCohortDrift("injected expected failure")
        return result

    monkeypatch.setattr(adapter._ResetPorts, method, fail_after)
    # Deliberately catch inside caller's transaction, as the actual reset route does.
    with pytest.raises(BadRequestError):
        _reset(reset_account)
    db.flush()
    assert token.used_at is sibling.used_at is None
    assert all(row.password_hash == "old-hash" for row in credentials)
    assert [row.revoked_at for row in sessions] == [None, None, NOW - timedelta(days=1)]
    assert not list(db.scalars(select(AuditEvent).where(AuditEvent.tenant_id == tid)))
    policies = list(db.scalars(select(AcademyCredentialPolicy).where(AcademyCredentialPolicy.tenant_id == tid)))
    assert sorted(row.lifecycle_version for row in policies) == [2, 7]
    assert all(row.reset_required and row.failed_login_attempts == 4 for row in policies)


def test_snapshot_uses_real_party_activity(reset_account, monkeypatch):
    observed = []
    original = adapter._ResetPorts._snapshot

    def observe(self, *args):
        snapshot = original(self, *args)
        observed.append(snapshot)
        return snapshot

    monkeypatch.setattr(adapter._ResetPorts, "_snapshot", observe)
    _reset(reset_account)
    assert len(observed) >= len(reset_account[3])
    assert all(row.is_active is False for row in observed)


def test_version_overflow_refuses_before_hash(reset_account, monkeypatch):
    import dotmac_kernel.credential_lifecycle as owner

    db = reset_account[0]
    policy = db.get(AcademyCredentialPolicy, reset_account[3][0].id)
    policy.lifecycle_version = 2**63 - 1
    db.flush()

    def forbidden(secret):
        raise AssertionError("hash reached")

    monkeypatch.setattr(owner, "hash_password", forbidden)
    with pytest.raises(BadRequestError):
        _reset(reset_account)
    assert reset_account[5].used_at is None


def test_actual_route_catches_bad_request_after_real_savepoint_rollback(reset_account, monkeypatch):
    from app.web import lifecycle as route

    db, tid, party, credentials, raw, token, sibling, _ = reset_account
    original = adapter._ResetPorts.invalidate_siblings

    def fail(self):
        original(self)
        raise adapter.CredentialCohortDrift("failure after every reset effect")

    monkeypatch.setattr(adapter._ResetPorts, "invalidate_siblings", fail)
    # Pin time and connect the proposed service seam; the actual route and
    # its caught-error response execute unchanged against the real PG Session.
    monkeypatch.setattr(
        route.lifecycle,
        "reset_password",
        lambda db, **kw: adapter.complete_password_reset(
            db, **kw, password_policy=AcademyPasswordPolicy(minimum_length=8), now=NOW
        ),
    )
    monkeypatch.setattr(route, "require_tenant", lambda request: SimpleNamespace(id=tid))
    request = Request({"type": "http", "headers": [(b"hx-request", b"true")]})
    response = route.reset_submit(request, token=raw, password="chosen-new9", db=db)
    assert response.status_code == 400
    assert db.is_active
    # Route returns normally: caller may flush/commit its root. All reset
    # effects must already be absent without a caller rollback.
    # This is the test's caller-owned boundary, never a service commit.
    db.commit()
    db.expire_all()
    assert token.used_at is sibling.used_at is None
    assert all(row.password_hash == "old-hash" for row in credentials)
    assert not list(db.scalars(select(AuditEvent).where(AuditEvent.tenant_id == tid)))
    assert party.email.startswith("Reset-")


def test_cas_zero_rows_rolls_back_consumed_proof(reset_account, monkeypatch):
    db = reset_account[0]
    original = db.execute

    def refuse(stmt, *args, **kwargs):
        if getattr(stmt, "is_update", False) and stmt.table.name == "academy_credential_policies":
            return SimpleNamespace(rowcount=0)
        return original(stmt, *args, **kwargs)

    monkeypatch.setattr(db, "execute", refuse)
    with pytest.raises(BadRequestError):
        _reset(reset_account)
    assert reset_account[5].used_at is None
    assert all(row.password_hash == "old-hash" for row in reset_account[3])


def test_unused_ports_explicitly_refuse(reset_account):
    ports = adapter._ResetPorts(reset_account[0], tenant_id=reset_account[1], raw=reset_account[4], now=NOW)
    with pytest.raises(adapter.CredentialLifecycleError):
        ports.load(None)
    with pytest.raises(adapter.CredentialLifecycleError):
        ports.create(None, password_hash="unused", reset_required=False)
    with pytest.raises(adapter.CredentialLifecycleError):
        ports.find_receipt(None)
    with pytest.raises(adapter.CredentialLifecycleError):
        ports.record_receipt(None)
    with pytest.raises(adapter.CredentialLifecycleError):
        adapter._Unsupported().emit(None)


def test_stale_identity_map_cannot_supply_activity_hash_or_version(reset_account, monkeypatch):
    db, _, party, credentials, *_ = reset_account
    policy = db.get(AcademyCredentialPolicy, credentials[0].id)
    db.execute(
        update(Party).where(Party.id == party.id).values(is_active=True).execution_options(synchronize_session=False)
    )
    db.execute(
        update(UserCredential)
        .where(UserCredential.id == credentials[0].id)
        .values(password_hash="fresh-hash")
        .execution_options(synchronize_session=False)
    )
    db.execute(
        update(AcademyCredentialPolicy)
        .where(AcademyCredentialPolicy.credential_id == credentials[0].id)
        .values(lifecycle_version=40)
        .execution_options(synchronize_session=False)
    )
    assert party.is_active is False
    assert policy.lifecycle_version == 2
    assert credentials[0].password_hash == "old-hash"
    observed = []
    original = adapter._ResetPorts._snapshot

    def observe(self, *args):
        snapshot = original(self, *args)
        observed.append(snapshot)
        return snapshot

    monkeypatch.setattr(adapter._ResetPorts, "_snapshot", observe)
    _reset(reset_account)
    assert all(row.is_active is True for row in observed)
    selected = [row for row in observed if row.subject.credential_ref == str(credentials[0].id)]
    assert selected and all(row.credential_version == 40 and row.password_hash == "fresh-hash" for row in selected)
    assert policy.lifecycle_version == 41


def test_missing_policy_refuses_complete_cohort_before_hash(reset_account, monkeypatch):
    import dotmac_kernel.credential_lifecycle as owner

    db = reset_account[0]
    db.delete(db.get(AcademyCredentialPolicy, reset_account[3][1].id))
    db.flush()

    def forbidden(secret):
        raise AssertionError("hash reached")

    monkeypatch.setattr(owner, "hash_password", forbidden)
    with pytest.raises(BadRequestError):
        _reset(reset_account)
    assert reset_account[5].used_at is None


def test_alias_conflict_before_hash_rolls_back_proof(reset_account, monkeypatch):
    import dotmac_kernel.credential_lifecycle as owner

    db, tid, party, *_ = reset_account
    other = Party(
        tenant_id=tid,
        party_type=PartyType.person,
        display_name="Other",
        email=f"other-{uuid4().hex}@a.edu",
        is_active=True,
    )
    db.add(other)
    db.flush()
    db.add(PartyPerson(party_id=other.id, first_name="Other", last_name="Subject"))
    db.flush()
    credential = UserCredential(tenant_id=tid, party_id=other.id, password_hash="unrelated-hash")
    db.add(credential)
    db.flush()
    db.add(
        AcademyCredentialPolicy(
            credential_id=credential.id,
            tenant_id=tid,
            email=party.email.lower(),
            failed_login_attempts=0,
            lifecycle_version=0,
            reset_required=False,
            locked_until=None,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    db.flush()

    def forbidden(secret):
        raise AssertionError("hash reached")

    monkeypatch.setattr(owner, "hash_password", forbidden)
    with pytest.raises(BadRequestError):
        _reset(reset_account)
    assert reset_account[5].used_at is None
    assert party.email.startswith("Reset-")
    assert credential.password_hash == "unrelated-hash"


@pytest.mark.parametrize(
    "minimum,material,accepted",
    [(8, "seven77", False), (8, "eight888", True), (12, "eight888", False), (5, "five5", True)],
)
def test_injected_configurable_policy_preserves_legacy_default_and_changes_when_configured(
    reset_account, minimum, material, accepted
):
    policy = AcademyPasswordPolicy(minimum_length=minimum)
    if accepted:
        _reset(reset_account, password=material, password_policy=policy)
    else:
        with pytest.raises(BadRequestError):
            _reset(reset_account, password=material, password_policy=policy)
        assert reset_account[5].used_at is None


def test_composed_policy_runs_before_proof_and_for_every_real_member(reset_account):
    class ObservedPolicy:
        def __init__(self):
            self.subjects = []
            self.account_checks = 0

        def validate_account(self, secret):
            self.account_checks += 1

        def validate(self, subject, secret):
            self.subjects.append(subject)

    policy = ObservedPolicy()
    _reset(reset_account, password_policy=policy)
    assert policy.account_checks == 1
    assert {row.credential_ref for row in policy.subjects} == {str(row.id) for row in reset_account[3]}


@pytest.mark.parametrize("expected", [False, True])
def test_early_policy_secret_bearing_exceptions_are_masked_before_proof(reset_account, monkeypatch, expected):
    material = f"material-{uuid4().hex}"

    class BrokenPolicy:
        def validate_account(self, secret):
            if expected:
                raise adapter.CredentialPolicyViolation(secret)
            raise ValueError(secret)

        def validate(self, subject, secret):
            raise AssertionError("subject policy must not be reached")

    def forbidden(*args, **kwargs):
        raise AssertionError("token proof must not be reached")

    monkeypatch.setattr(adapter, "_candidate", forbidden)
    exception_type = BadRequestError if expected else adapter.CredentialLifecycleError
    with pytest.raises(exception_type) as error:
        _reset(reset_account, password=material, password_policy=BrokenPolicy())
    assert material not in str(error.value)
    assert material not in repr(error.value)
    assert error.value.__suppress_context__
    assert reset_account[5].used_at is None
