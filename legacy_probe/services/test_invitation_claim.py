"""Authored hosted PostgreSQL canaries; no local execution or fixture shim.

The actual legacy AccountToken FK still blocks this cohort. These tests require
the genuine migrated product app_user fixture and intentionally fail if absent.
"""

import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from dotmac_kernel.audit import AuditEvent
from dotmac_kernel.credential_lifecycle import CredentialAssignmentAuditEntryV1, CredentialAssignmentError
from dotmac_kernel.models import AuthSession, Party, PartyPerson, PartyRoleGrant, PartyType, UserCredential
from dotmac_kernel.security import verify_password
from dotmac_kernel.session_runtime import resolve_database_runtime
from sqlalchemy import inspect, select, text

from app.models.account_token import AccountToken
from app.models.identity_extensions import AcademyAccountProfile, AcademyCredentialPolicy
from app.services import invitation_claim as adapter
from app.services.credential_policy import AcademyPasswordPolicy
from app.services.exceptions import BadRequestError, ConflictError
from app.services.security import hash_token

NOW = datetime(2026, 10, 18, 12, tzinfo=UTC)


@pytest.fixture
def invitation_account(request):
    assert os.getenv("CI") == "true", "hosted CI only"
    assert os.getenv("TEST_DATABASE_URL") and os.getenv("TEST_MIGRATION_DATABASE_URL")
    request.getfixturevalue("app_client")
    tenant = request.getfixturevalue("tenant_a")
    admin = request.getfixturevalue("admin_session")
    db = request.getfixturevalue("app_user_session")
    assert db.get_bind().dialect.name == "postgresql"
    assert not db.execute(text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user")).scalar_one()
    fks = inspect(admin.get_bind()).get_foreign_keys("account_tokens", schema="public")
    assert any(
        fk["referred_table"] == "parties" and fk["constrained_columns"] == ["tenant_id", "person_id"] for fk in fks
    )
    assert not any(fk["referred_table"] == "people" for fk in fks), "legacy token FK remains unresolved"

    resolve_database_runtime().set_tenant(db, tenant.id)
    party = Party(
        id=uuid4(),
        tenant_id=tenant.id,
        party_type=PartyType.person,
        display_name="Invitation",
        email=f"Invite-{uuid4().hex}@a.edu",
        is_active=False,
        created_at=NOW,
        updated_at=NOW,
    )
    db.add(party)
    db.flush()
    db.add(PartyPerson(party_id=party.id, first_name="Invitation", last_name="Subject"))
    db.flush()
    profile = AcademyAccountProfile(
        tenant_id=tenant.id, party_id=party.id, status="suspended", prefs={}, created_at=NOW, updated_at=NOW
    )
    raw = uuid4().hex + uuid4().hex
    token = AccountToken(
        tenant_id=tenant.id, person_id=party.id, kind="invite", token_hash=hash_token(raw), expires_at=NOW
    )
    sibling = AccountToken(
        tenant_id=tenant.id,
        person_id=party.id,
        kind="invite",
        token_hash=hash_token(uuid4().hex),
        expires_at=NOW - timedelta(days=1),
    )
    db.add_all([profile, token, sibling])
    db.flush()
    # Explicit injected policy for this bounded adapter; production must obtain
    # the legacy minimum from its validated Settings/env policy composition.
    return db, tenant.id, party, profile, raw, token, sibling, AcademyPasswordPolicy(minimum_length=8)


def claim(account, **kwargs):
    db, tid, _, _, raw, _, _, policy = account
    return adapter.accept_invite(
        db,
        tenant_id=tid,
        raw=kwargs.get("raw", raw),
        password=kwargs.get("password", "chosen-new9"),
        password_policy=policy,
        now=NOW,
    )


def test_genuine_roleless_inactive_suspended_claim_at_equality_expiry(invitation_account):
    db, tid, party, profile, raw, token, sibling, _ = invitation_account
    assert claim(invitation_account).id == party.id
    credential = db.scalars(select(UserCredential).where(UserCredential.party_id == party.id)).one()
    policy = db.get(AcademyCredentialPolicy, credential.id)
    assert verify_password("chosen-new9", credential.password_hash)
    assert policy.lifecycle_version == 0 and policy.reset_required is False
    assert policy.failed_login_attempts == 0 and policy.locked_until is None
    assert party.is_active is False and profile.status == "suspended"
    assert token.used_at == NOW and sibling.used_at == NOW
    event = db.scalars(
        select(AuditEvent).where(AuditEvent.tenant_id == tid, AuditEvent.entity_id == str(credential.id))
    ).one()
    assert event.actor_type == "user" and event.actor_id == str(party.id)
    assert event.actor_party_id == party.id
    assert event.details["authority_evidence_ref"] == str(token.id)
    assert all(
        material not in repr(event.details)
        for material in (raw, hash_token(raw), "chosen-new9", credential.password_hash)
    )
    with pytest.raises(BadRequestError):
        claim(invitation_account)
    assert db.is_active


def test_any_existing_account_credential_refuses_claim(invitation_account):
    db, tid, party, _, _, token, sibling, _ = invitation_account
    db.add(
        UserCredential(
            id=uuid4(),
            tenant_id=tid,
            party_id=party.id,
            password_hash="existing-material",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    db.flush()
    with pytest.raises(ConflictError):
        claim(invitation_account)
    assert token.used_at is None and sibling.used_at is None and db.is_active


def test_password_semantics_precede_token_mutation(invitation_account):
    db, _, _, _, _, token, sibling, _ = invitation_account
    with pytest.raises(BadRequestError):
        claim(invitation_account, password="short")
    assert token.used_at is None and sibling.used_at is None and db.is_active


@pytest.mark.parametrize("mutation", ["kind", "hash", "expired", "used"])
def test_genuine_token_binding_refuses_invalid_proof(invitation_account, mutation):
    db, _, _, _, _, token, sibling, _ = invitation_account
    if mutation == "kind":
        token.kind = "password_reset"
    elif mutation == "hash":
        token.token_hash = hash_token("different-bearer")
    elif mutation == "expired":
        token.expires_at = NOW - timedelta(microseconds=1)
    else:
        token.used_at = NOW - timedelta(days=1)
    db.flush()
    with pytest.raises(BadRequestError):
        claim(invitation_account)
    assert sibling.used_at is None and db.is_active


def test_post_cohort_wait_clock_recheck(invitation_account, monkeypatch):
    original = adapter._InvitationClaimPorts._absent

    def crossed_wait(self, command, **kwargs):
        result = original(self, command, **kwargs)
        self.clock = lambda: NOW + timedelta(microseconds=1)
        return result

    monkeypatch.setattr(adapter._InvitationClaimPorts, "_absent", crossed_wait)
    db, _, _, _, _, token, sibling, _ = invitation_account
    with pytest.raises(BadRequestError):
        claim(invitation_account)
    assert token.used_at is None and sibling.used_at is None and db.is_active


def test_audit_failure_caught_outer_commit_has_zero_claim_effects(invitation_account, monkeypatch):
    db, tid, party, _, _, token, sibling, _ = invitation_account
    root = db.get_transaction()
    party_id, token_id, sibling_id = party.id, token.id, sibling.id

    def failed_audit(*args, **kwargs):
        raise RuntimeError("injected audit failure")

    monkeypatch.setattr(adapter, "write_audit_event", failed_audit)
    with pytest.raises(CredentialAssignmentError):
        claim(invitation_account)
    assert db.is_active and db.get_transaction() is root
    assert db.execute(text("SELECT 1")).scalar_one() == 1
    db.commit()  # The product fixture must support a genuine caller commit.

    resolve_database_runtime().set_tenant(db, tid)  # Caller commit cleared SET LOCAL RLS tenant context.
    readback_root = db.get_transaction()
    tokens = db.scalars(
        select(AccountToken)
        .where(
            AccountToken.tenant_id == tid,
            AccountToken.person_id == party_id,
            AccountToken.kind == "invite",
        )
        .execution_options(populate_existing=True)
    ).all()
    assert {row.id: row.used_at for row in tokens} == {token_id: None, sibling_id: None}
    assert not db.scalars(select(UserCredential).where(UserCredential.party_id == party_id)).all()
    assert not db.scalars(
        select(AcademyCredentialPolicy).where(
            AcademyCredentialPolicy.tenant_id == tid,
            AcademyCredentialPolicy.email == party.email.strip().lower(),
        )
    ).all()
    assert not db.scalars(
        select(AuditEvent).where(
            AuditEvent.tenant_id == tid,
            AuditEvent.actor_party_id == party_id,
            AuditEvent.source_application == adapter.APPLICATION,
        )
    ).all()
    assert db.execute(text("SELECT 1")).scalar_one() == 1
    assert db.is_active and readback_root is not None and db.get_transaction() is readback_root


def test_hostile_tenant_cannot_claim_genuine_other_tenant_token(invitation_account):
    db, _, party, _, raw, token, sibling, policy = invitation_account
    root = db.get_transaction()
    with pytest.raises(BadRequestError):
        adapter.accept_invite(db, tenant_id=uuid4(), raw=raw, password="chosen-new9", password_policy=policy, now=NOW)
    assert db.is_active and db.get_transaction() is root
    assert token.used_at is None and sibling.used_at is None
    assert not db.scalars(select(UserCredential).where(UserCredential.party_id == party.id)).all()


@pytest.mark.parametrize("mutation", ["copied_evidence", "copied_command", "subject", "cross_root", "cleared"])
def test_genuine_private_authority_refuses_forged_or_stale_context(invitation_account, mutation):
    db, tid, party, _, raw, token, sibling, policy = invitation_account
    root = db.get_transaction()
    with db.begin_nested() as savepoint:
        ports = adapter._InvitationClaimPorts(
            db, tenant_id=tid, digest=hash_token(raw), policy=policy, clock=lambda: NOW
        )
        try:
            command = ports.prepare()
            evidence = ports.authorize_and_lock(command)
            entry = CredentialAssignmentAuditEntryV1(command, evidence, None, 0, NOW)
            if mutation == "copied_evidence":
                entry = replace(entry, authority=replace(evidence))
            elif mutation == "copied_command":
                copied = replace(command)
                entry = replace(entry, command=copied, authority=replace(evidence, command=copied))
            elif mutation == "subject":
                copied = replace(command, target_subject=replace(command.target_subject, principal_ref=str(uuid4())))
                entry = replace(entry, command=copied, authority=replace(evidence, command=copied))
            elif mutation == "cross_root":
                ports.root = object()
            else:
                ports.clear()
            with pytest.raises(CredentialAssignmentError):
                ports.record_assignment(entry)
        finally:
            ports.clear()
            savepoint.rollback()
    assert db.is_active and db.get_transaction() is root
    db.refresh(token)
    db.refresh(sibling)
    assert token.used_at is None and sibling.used_at is None
    assert not db.scalars(select(UserCredential).where(UserCredential.party_id == party.id)).all()


def test_existing_credential_refusal_preserves_all_original_account_state(invitation_account):
    db, tid, party, profile, _, token, sibling, _ = invitation_account
    original_hash = "existing-private-hash"
    credential = UserCredential(
        id=uuid4(), tenant_id=tid, party_id=party.id, password_hash=original_hash, created_at=NOW, updated_at=NOW
    )
    db.add(credential)
    db.flush()
    policy = AcademyCredentialPolicy(
        credential_id=credential.id,
        tenant_id=tid,
        email=party.email,
        failed_login_attempts=4,
        locked_until=NOW + timedelta(hours=1),
        lifecycle_version=7,
        reset_required=True,
        created_at=NOW,
        updated_at=NOW,
    )
    session = AuthSession(
        id=uuid4(),
        tenant_id=tid,
        party_id=party.id,
        token_hash=hash_token(uuid4().hex),
        expires_at=NOW + timedelta(hours=1),
        revoked_at=None,
        created_at=NOW,
        updated_at=NOW,
    )
    db.add_all([policy, session])
    db.flush()
    grants = tuple(db.scalars(select(PartyRoleGrant.id).where(PartyRoleGrant.party_id == party.id)).all())
    state = (
        party.email,
        party.is_active,
        profile.status,
        policy.email,
        policy.lifecycle_version,
        policy.reset_required,
        policy.failed_login_attempts,
        policy.locked_until,
        session.token_hash,
        session.expires_at,
        session.revoked_at,
    )
    root = db.get_transaction()
    with pytest.raises(ConflictError):
        claim(invitation_account)
    for row in (party, profile, credential, policy, session, token, sibling):
        db.refresh(row)
    assert credential.password_hash == original_hash
    assert (
        party.email,
        party.is_active,
        profile.status,
        policy.email,
        policy.lifecycle_version,
        policy.reset_required,
        policy.failed_login_attempts,
        policy.locked_until,
        session.token_hash,
        session.expires_at,
        session.revoked_at,
    ) == state
    assert tuple(db.scalars(select(PartyRoleGrant.id).where(PartyRoleGrant.party_id == party.id)).all()) == grants
    assert token.used_at is None and sibling.used_at is None
    assert db.is_active and db.get_transaction() is root
