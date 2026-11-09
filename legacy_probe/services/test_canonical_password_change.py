"""Hosted genuine canonical password-change acceptance; authored, NOT RUN.

AccountToken rows below require the separately prepared genuine canonical FK
adoption. No old Person, schema shim, skip, runtime replacement or RLS bypass is
used. Academy has no credential.is_active field; abstract credential eligibility
is qualified by the Kernel owner, while Party/profile eligibility is tested here.
"""

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import dotmac_kernel.credential_lifecycle as owner
import pytest
from dotmac_kernel.audit import AuditEvent
from dotmac_kernel.models import AuthSession, Party, PartyPerson, UserCredential
from dotmac_kernel.security import password_needs_rehash, verify_password
from dotmac_kernel.session_runtime import resolve_database_runtime
from fastapi import HTTPException
from sqlalchemy import select, text, update

from app.models.account_token import AccountToken
from app.models.identity_extensions import AcademyAccountProfile, AcademyCredentialPolicy
from app.services import password_change as adapter
from app.services import settings_store
from app.services.security import hash_password, hash_token
from legacy_probe.services.test_canonical_current_user import _request, current_account

assert current_account


def _change(a, *, current="chosen-password9", new="replacement9", confirmation=None, request=None):
    return adapter.change_password(
        a.db,
        request=request or _request(a.tenant, a.token),
        current_password=current,
        new_password=new,
        confirm_password=new if confirmation is None else confirmation,
    )


def _alias(a, *, secret="chosen-password9", version=7):
    row = UserCredential(tenant_id=a.tid, party_id=a.pid, password_hash=hash_password(secret))
    a.db.add(row)
    a.db.flush()
    now = datetime.now(UTC)
    policy = AcademyCredentialPolicy(
        tenant_id=a.tid,
        credential_id=row.id,
        email=f"alias-{uuid4().hex}@a.edu",
        failed_login_attempts=4,
        locked_until=now + timedelta(hours=1),
        lifecycle_version=version,
        reset_required=True,
        created_at=now,
        updated_at=now,
    )
    a.db.add(policy)
    a.db.flush()
    return row, policy


def _selected(a):
    return a.db.scalars(
        select(UserCredential).where(UserCredential.tenant_id == a.tid, UserCredential.party_id == a.pid)
    ).first()


def _state(a):
    a.db.expire_all()
    credentials = a.db.scalars(
        select(UserCredential).where(UserCredential.party_id == a.pid).order_by(UserCredential.id)
    ).all()
    policies = a.db.scalars(
        select(AcademyCredentialPolicy)
        .where(
            AcademyCredentialPolicy.tenant_id == a.tid,
            AcademyCredentialPolicy.credential_id.in_([row.id for row in credentials]),
        )
        .order_by(AcademyCredentialPolicy.credential_id)
    ).all()
    party, person, profile = (
        a.db.get(Party, a.pid),
        a.db.get(PartyPerson, a.pid),
        a.db.get(AcademyAccountProfile, a.pid),
    )
    return (
        [(row.id, row.password_hash) for row in credentials],
        [
            (
                row.credential_id,
                row.email,
                row.failed_login_attempts,
                row.locked_until,
                row.lifecycle_version,
                row.reset_required,
                row.updated_at,
            )
            for row in policies
        ],
        (
            party.email,
            party.display_name,
            party.is_active,
            person.first_name,
            person.last_name,
            profile.status,
            profile.avatar_path,
            profile.prefs,
            profile.updated_at,
        ),
        [
            (row.id, row.expires_at, row.revoked_at)
            for row in a.db.scalars(
                select(AuthSession).where(AuthSession.party_id == a.pid).order_by(AuthSession.id)
            ).all()
        ],
        [
            (row.id, row.kind, row.expires_at, row.used_at)
            for row in a.db.scalars(
                select(AccountToken).where(AccountToken.person_id == a.pid).order_by(AccountToken.id)
            ).all()
        ],
        [
            row.id
            for row in a.db.scalars(
                select(AuditEvent)
                .where(AuditEvent.tenant_id == a.tid, AuditEvent.entity_id == str(a.pid))
                .order_by(AuditEvent.id)
            ).all()
        ],
    )


def _commit_readback(a):
    a.db.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert a.db.scalar(text("SELECT current_user")) == "app_user"
    assert a.db.scalar(text("SELECT current_setting('app.current_tenant')")) == str(a.tid)
    return _state(a)


@pytest.mark.parametrize(
    "current,new,confirmation,code",
    [
        ("wrong", "x", "different", "current_password_invalid"),
        ("chosen-password9", "x", "different", "confirmation_mismatch"),
        ("chosen-password9", "x", "x", "policy_rejected"),
    ],
)
def test_error_precedence_and_zero_effects_survive_caught_outer_commit(
    current_account, current, new, confirmation, code
):
    a = current_account
    _alias(a)
    before = _state(a)
    root = a.db.get_transaction()
    result = _change(a, current=current, new=new, confirmation=confirmation)
    assert result.code == code and not result.ok
    if code == "policy_rejected":
        assert result.min_length == 8
    assert a.db.get_transaction() is root and a.db.is_active
    assert _commit_readback(a) == before


def test_confirmation_is_checked_before_strict_settings_but_after_old_match(current_account, monkeypatch):
    a = current_account
    calls = []

    def unavailable(db):
        calls.append(True)
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(adapter, "password_change_policy", unavailable)
    assert _change(a, current="wrong", confirmation="different").code == "current_password_invalid"
    assert _change(a, confirmation="different").code == "confirmation_mismatch"
    assert calls == []
    before = _state(a)
    with pytest.raises(owner.CredentialAuthenticatedAccountPasswordChangeError):
        _change(a)
    assert calls == [True]
    assert _commit_readback(a) == before


def test_resolved_purpose_minimum_is_used_without_creation_or_reset_substitution(current_account, monkeypatch):
    monkeypatch.setattr(
        settings_store,
        "get_all",
        lambda db: {
            "password_change_min_length": "12",
            "web_account_creation_password_min_length": "0",
            "password_reset_min_length": "8",
        },
    )
    result = _change(current_account, new="123456789")
    assert result.code == "policy_rejected" and result.min_length == 12


def test_native_unordered_selected_alias_only_and_one_new_hash_without_old_rehash(current_account, monkeypatch):
    a = current_account
    _alias(a)
    selected = _selected(a)
    salt = b"known-test-salt!"
    digest = hashlib.pbkdf2_hmac("sha256", b"selected-old9", salt, 1)
    selected.password_hash = (
        f"pbkdf2_sha256$1${base64.urlsafe_b64encode(salt).decode()}${base64.urlsafe_b64encode(digest).decode()}"
    )
    a.db.flush()
    assert password_needs_rehash(selected.password_hash)
    before = _state(a)
    # The nonselected alias still has another valid password, which cannot
    # satisfy the selected old comparison.
    other = next(
        row
        for row in a.db.scalars(select(UserCredential).where(UserCredential.party_id == a.pid)).all()
        if row.id != selected.id
    )
    other.password_hash = hash_password("unselected-old9")
    a.db.flush()
    assert _change(a, current="unselected-old9").code == "current_password_invalid"
    calls = []
    original_hash = owner.hash_password

    def new_hash(secret):
        assert secret == "replacement9"
        calls.append(True)
        return original_hash(secret)

    def forbidden_rehash(*args):
        raise AssertionError("old-password rehash path was called")

    monkeypatch.setattr(owner, "hash_password", new_hash)
    monkeypatch.setattr(owner, "password_needs_rehash", forbidden_rehash)
    assert _change(a, current="selected-old9").ok
    assert calls == [True]
    after = _state(a)
    assert {row[0] for row in after[0]} == {row[0] for row in before[0]}
    assert len({row[1] for row in after[0]}) == 1
    assert all(verify_password("replacement9", row[1]) for row in after[0])


def test_valid_cookie_allows_locked_reset_required_credentials_and_clears_all_aliases(current_account):
    a = current_account
    extra, extra_policy = _alias(a)
    primary_policy = a.db.get(AcademyCredentialPolicy, a.credential_id)
    primary_policy.failed_login_attempts, primary_policy.reset_required = 9, True
    primary_policy.locked_until = datetime.now(UTC) + timedelta(days=1)
    a.db.flush()
    before = _state(a)
    assert _change(a).ok
    after = _state(a)
    versions = {row[0]: row[4] for row in before[1]}
    assert {row[0] for row in after[0]} == {a.credential_id, extra.id}
    for row in after[1]:
        assert row[2] == 0 and row[3] is None and row[5] is False
        assert row[4] == versions[row[0]] + 1
    assert next(row[1] for row in after[1] if row[0] == extra.id) == extra_policy.email
    assert after[2] == before[2]


def test_success_preserves_used_other_tokens_and_old_revocations_consumes_all_unused_reset_even_expired(
    current_account,
):
    a = current_account
    now = datetime.now(UTC)
    old_revoked = now - timedelta(hours=2)
    for expiry, revoked in [(now - timedelta(hours=1), None), (now + timedelta(hours=1), None), (now, old_revoked)]:
        a.db.add(
            AuthSession(
                tenant_id=a.tid,
                party_id=a.pid,
                token_hash=hash_token(uuid4().hex),
                expires_at=expiry,
                revoked_at=revoked,
            )
        )
    for kind, used, expiry in [
        ("password_reset", None, now - timedelta(days=1)),
        ("password_reset", None, now + timedelta(days=1)),
        ("password_reset", old_revoked, now),
        ("invite", None, now),
        ("email_verify", None, now),
    ]:
        a.db.add(
            AccountToken(
                tenant_id=a.tid,
                person_id=a.pid,
                kind=kind,
                used_at=used,
                expires_at=expiry,
                token_hash=hash_token(uuid4().hex),
            )
        )
    a.db.flush()  # Requires genuine canonical AccountToken FK adoption.
    before = _state(a)
    assert _change(a).ok
    after = _state(a)
    prior_sessions = {row[0]: row for row in before[3]}
    for row in after[3]:
        assert row[1] == prior_sessions[row[0]][1]
        assert row[2] is not None
        if prior_sessions[row[0]][2] is not None:
            assert row[2] == prior_sessions[row[0]][2]
    prior_tokens = {row[0]: row for row in before[4]}
    for row in after[4]:
        prior = prior_tokens[row[0]]
        if prior[1] == "password_reset" and prior[3] is None:
            assert row[3] is not None
            assert row[:3] == prior[:3]
        else:
            assert row == prior
    events = a.db.scalars(
        select(AuditEvent).where(AuditEvent.tenant_id == a.tid, AuditEvent.entity_id == str(a.pid))
    ).all()
    assert {row.action for row in events} == {
        "credential.sessions.revoked",
        "credential.reset_tokens.invalidated",
        "credential.password.changed",
    }
    final = next(row for row in events if row.action == "credential.password.changed")
    assert final.actor_type == "user" and final.actor_id == str(a.pid) and final.actor_party_id == a.pid
    assert final.details["invalidated_reset_token_count"] == 2
    assert final.details["session_completion_ref"] in {
        str(row.id) for row in events if row.action == "credential.sessions.revoked"
    }
    assert final.details["reset_token_completion_ref"] in {
        str(row.id) for row in events if row.action == "credential.reset_tokens.invalidated"
    }
    assert "replacement9" not in repr([row.details for row in events])
    assert not any("hash" in key for row in events for key in row.details)


@pytest.mark.parametrize(
    "stage",
    [
        "synchronize_account_aliases",
        "write_hash",
        "clear_failure_state",
        "revoke_sessions",
        "invalidate_outstanding_reset_tokens",
        "record_change",
    ],
)
def test_every_effect_stage_failure_rolls_back_all_effects_and_audits_under_same_caller_root(
    current_account, monkeypatch, stage
):
    a = current_account
    _alias(a)
    now = datetime.now(UTC)
    unused_resets = [
        AccountToken(
            tenant_id=a.tid,
            person_id=a.pid,
            kind="password_reset",
            used_at=None,
            expires_at=now + delta,
            token_hash=hash_token(uuid4().hex),
        )
        for delta in (timedelta(days=-1), timedelta(days=1))
    ]
    other_tokens = [
        AccountToken(
            tenant_id=a.tid,
            person_id=a.pid,
            kind=kind,
            used_at=used_at,
            expires_at=now + timedelta(days=1),
            token_hash=hash_token(uuid4().hex),
        )
        for kind, used_at in (("password_reset", now - timedelta(hours=2)), ("invite", None), ("email_verify", None))
    ]
    # Real canonical Party UUIDs; these rows require the separately prepared
    # AccountToken FK retarget. No legacy Person/shim is used to make setup pass.
    a.db.add_all([*unused_resets, *other_tokens])
    a.db.flush()
    unused_ids = {row.id for row in unused_resets}
    before = _state(a)
    root = a.db.get_transaction()
    original = getattr(adapter._ChangePorts, stage)
    observed_spends = []

    def fail_after(self, *args, **kwargs):
        original(self, *args, **kwargs)
        if stage in ("invalidate_outstanding_reset_tokens", "record_change"):
            spent = self.db.scalars(
                select(AccountToken)
                .where(
                    AccountToken.tenant_id == a.tid,
                    AccountToken.person_id == a.pid,
                    AccountToken.id.in_(unused_ids),
                )
                .execution_options(populate_existing=True)
            ).all()
            assert len(spent) == 2 and all(row.used_at is not None for row in spent)
            observed_spends.append(len(spent))
        raise RuntimeError("injected after owned stage")

    monkeypatch.setattr(adapter._ChangePorts, stage, fail_after)
    with pytest.raises(owner.CredentialAuthenticatedAccountPasswordChangeError):
        _change(a)
    assert a.db.get_transaction() is root and a.db.is_active
    assert _commit_readback(a) == before
    # _commit_readback commits the caught failure and restores app.current_tenant
    # before RLS readback. Both live and expired token spends must be restored.
    restored = a.db.scalars(
        select(AccountToken)
        .where(
            AccountToken.tenant_id == a.tid,
            AccountToken.person_id == a.pid,
            AccountToken.id.in_(unused_ids),
        )
        .execution_options(populate_existing=True)
    ).all()
    assert {row.id for row in restored} == unused_ids
    assert all(row.used_at is None for row in restored)
    assert observed_spends == ([2] if stage in ("invalidate_outstanding_reset_tokens", "record_change") else [])


@pytest.mark.parametrize("cookie", [None, "unpersisted", "tampered"])
def test_current_actual_cookie_and_tenant_are_required_even_with_stale_guard_projection(
    current_account, tenant_b, cookie
):
    a = current_account
    before = _state(a)
    raw = a.token + "changed" if cookie == "tampered" else cookie
    for request in [_request(a.tenant, raw), _request(tenant_b, a.token)]:
        with pytest.raises(HTTPException) as caught:
            _change(a, request=request)
        assert caught.value.status_code == 303 and caught.value.headers == {"Location": "/login"}
    assert _commit_readback(a) == before


@pytest.mark.parametrize("restriction", ["revoked", "expired", "inactive", "suspended"])
def test_fresh_authority_refuses_stale_cached_session_party_profile(current_account, restriction):
    a = current_account
    a.db.get(AuthSession, a.session_id), a.db.get(Party, a.pid), a.db.get(AcademyAccountProfile, a.pid)
    if restriction == "revoked":
        a.admin.execute(update(AuthSession).where(AuthSession.id == a.session_id).values(revoked_at=datetime.now(UTC)))
    elif restriction == "expired":
        a.admin.execute(
            update(AuthSession)
            .where(AuthSession.id == a.session_id)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    elif restriction == "inactive":
        a.admin.execute(update(Party).where(Party.id == a.pid).values(is_active=False))
    else:
        a.admin.execute(
            update(AcademyAccountProfile).where(AcademyAccountProfile.party_id == a.pid).values(status="suspended")
        )
    a.admin.commit()
    before = _state(a)
    with pytest.raises(HTTPException) as caught:
        _change(a)
    assert caught.value.status_code == 303
    assert _commit_readback(a) == before


def test_missing_credential_is_truthful_expected_old_password_refusal(current_account):
    a = current_account
    a.db.delete(a.db.get(AcademyCredentialPolicy, a.credential_id))
    a.db.delete(a.db.get(UserCredential, a.credential_id))
    a.db.flush()
    before = _state(a)
    assert _change(a, current="anything", confirmation="mismatch").code == "current_password_invalid"
    assert _commit_readback(a) == before


def test_alias_normalization_is_deferred_until_success_and_preserves_unrelated_aliases(current_account):
    a = current_account
    _extra, extra_policy = _alias(a)
    original_email = a.email
    a.db.get(Party, a.pid).email = f" {original_email.upper()} "
    primary = a.db.get(AcademyCredentialPolicy, a.credential_id)
    unrelated = f"unrelated-{uuid4().hex}@a.edu"
    primary.email = unrelated
    extra_policy.email = f" {original_email.upper()} "
    a.db.flush()
    before = _state(a)
    assert _change(a, current="wrong").code == "current_password_invalid"
    assert _state(a) == before
    assert _change(a).ok
    assert a.db.get(Party, a.pid).email == original_email
    assert primary.email == unrelated
    assert extra_policy.email == original_email


@pytest.mark.parametrize("attack", ["copied_evidence", "copied_command", "omitted_member", "version_cas_drift"])
def test_private_authority_complete_membership_and_named_cas_cannot_be_replaced(current_account, monkeypatch, attack):
    from dataclasses import replace

    a = current_account
    _alias(a)
    before = _state(a)
    if attack in ("copied_evidence", "copied_command"):
        original = adapter._ChangePorts.authorize_and_lock

        def substitute(self, command):
            if attack == "copied_command":
                return original(self, replace(command))
            return replace(original(self, command))

        monkeypatch.setattr(adapter._ChangePorts, "authorize_and_lock", substitute)
    elif attack == "omitted_member":
        original = adapter._ChangePorts.revalidate_before_change

        def omitted(self, evidence, credentials):
            return original(self, evidence, credentials[:-1])

        monkeypatch.setattr(adapter._ChangePorts, "revalidate_before_change", omitted)
    else:
        original = adapter._ChangePorts.write_hash

        def drift(self, subject, **kwargs):
            policy = self.members[subject][1]
            policy.lifecycle_version += 1
            self.db.flush()
            return original(self, subject, **kwargs)

        monkeypatch.setattr(adapter._ChangePorts, "write_hash", drift)
    with pytest.raises(owner.CredentialAuthenticatedAccountPasswordChangeError):
        _change(a)
    assert _commit_readback(a) == before


def test_version_exhaustion_refuses_without_partial_effects(current_account):
    a = current_account
    _alias(a, version=9223372036854775807)
    before = _state(a)
    with pytest.raises(owner.CredentialAuthenticatedAccountPasswordChangeError):
        _change(a)
    assert _commit_readback(a) == before


def test_cleared_private_context_cannot_reauthorize_equal_material_free_command(current_account):
    a = current_account
    digest = hash_token(a.token)
    ports = adapter._ChangePorts(
        a.db, tenant_id=a.tid, party_id=a.pid, session_id=a.session_id, digest=digest, confirmation="replacement9"
    )
    command = ports.command
    evidence = ports.authorize_and_lock(command)
    ports.clear_context()
    assert ports.digest is None and ports.db is None and ports.root is None and ports.authority is None
    assert ports.policy.confirmation is None and ports.policy.ports is None
    with pytest.raises(owner.CredentialAuthenticatedAccountPasswordChangeError):
        ports.authorize_and_lock(command)
    with pytest.raises(owner.CredentialAuthenticatedAccountPasswordChangeError):
        ports.revalidate_before_change(evidence, ())
