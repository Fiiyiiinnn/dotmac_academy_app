"""Assignment canaries. Hosted canonical PostgreSQL prerequisites fail closed.

These are authored source, not installed/runtime qualification evidence.
"""

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from dotmac_kernel.credential_lifecycle import (
    CredentialAssignmentAuditEntryV1,
    CredentialAssignmentError,
    CredentialSnapshot,
)
from dotmac_kernel.session_runtime import resolve_database_runtime

from app.services import credential_assignment as service


class BoundarySession:
    def __init__(self):
        self.root = object()
        self.is_active = True

    def get_transaction(self):
        return self.root


def bound_ports():
    db = BoundarySession()
    ports = service._WebAssignmentPorts(db, tenant_id=uuid4(), digest="private-digest", policy=object())
    ports.actor = SimpleNamespace(id=uuid4())
    ports.target = SimpleNamespace(id=uuid4())
    ports.credential_id = uuid4()
    ports.target_role = SimpleNamespace(id=uuid4())
    ports.command = ports._new_command()
    ports.evidence = service.CredentialAssignmentAuthorityEvidenceV1(
        ports.command, "user", str(ports.actor.id), str(uuid4())
    )
    return db, ports


@pytest.mark.parametrize("mutation", ["copy", "command", "root", "session", "inactive", "cleared"])
def test_private_binding_refuses_reconstructed_or_stale_authority(mutation, monkeypatch):
    monkeypatch.setattr(service, "conflict_savepoint_requires_rollback", lambda *a, **k: False)
    db, ports = bound_ports()
    entry = CredentialAssignmentAuditEntryV1(ports.command, ports.evidence, None, 0, datetime.now(UTC))
    if mutation == "copy":
        entry = replace(entry, authority=replace(entry.authority))
    elif mutation == "command":
        command = replace(ports.command)
        entry = replace(entry, command=command, authority=replace(entry.authority, command=command))
    elif mutation == "root":
        db.root = object()
    elif mutation == "session":
        ports.db = BoundarySession()
        ports.db.root = db.root
    elif mutation == "inactive":
        db.is_active = False
    else:
        ports.clear()
    with pytest.raises(CredentialAssignmentError):
        ports.record_assignment(entry)
    assert ports.evidence is None


def test_web_slice_never_accepts_replacement():
    _, ports = bound_ports()
    with pytest.raises(CredentialAssignmentError):
        ports.replace_selected(None, password_hash="material", credential_version=1, reset_required=False)


@pytest.mark.parametrize("mutation", ["hash", "version", "reset", "subject"])
def test_private_audit_rejects_changed_store_outcome(monkeypatch, mutation):
    monkeypatch.setattr(service, "conflict_savepoint_requires_rollback", lambda *a, **k: False)
    _, ports = bound_ports()
    original = CredentialSnapshot(ports.command.target_subject, "private-hash", 0, reset_required=False)
    ports._outcome = original
    values = {
        "hash": {"password_hash": "changed-hash"},
        "version": {"credential_version": 1},
        "reset": {"reset_required": True},
        "subject": {"subject": replace(original.subject, credential_ref=str(uuid4()))},
    }
    monkeypatch.setattr(ports, "_persisted_snapshot", lambda command: replace(original, **values[mutation]))
    writes = []
    monkeypatch.setattr(service, "write_audit_event", lambda *a, **k: writes.append(True))
    entry = CredentialAssignmentAuditEntryV1(ports.command, ports.evidence, None, 0, datetime.now(UTC))
    with pytest.raises(CredentialAssignmentError):
        ports.record_assignment(entry)
    assert not writes and ports.evidence is None and ports._outcome is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("revoked_at", datetime.now(UTC)),
        ("expires_at", datetime.now(UTC)),
        ("token_hash", "tampered"),
        ("tenant_id", uuid4()),
        ("party_id", uuid4()),
    ],
)
def test_refreshed_session_state_is_required(field, value):
    _, ports = bound_ports()
    session = SimpleNamespace(
        tenant_id=ports.tenant_id,
        party_id=ports.actor.id,
        revoked_at=None,
        expires_at=datetime.max.replace(tzinfo=UTC),
        token_hash="private-digest",
    )
    setattr(session, field, value)
    assert not ports._session_valid(session, datetime.now(UTC))


def test_unknown_failures_are_material_masked_after_savepoint(monkeypatch):
    from contextlib import contextmanager

    db = BoundarySession()
    exited = []

    @contextmanager
    def savepoint(db):
        try:
            yield
        finally:
            exited.append(True)

    monkeypatch.setattr(service, "require_tenant", lambda request: SimpleNamespace(id=uuid4()))
    monkeypatch.setattr(service, "conflict_savepoint", savepoint)
    monkeypatch.setattr(service, "conflict_savepoint_requires_rollback", lambda *a, **k: False)
    monkeypatch.setattr(service, "hash_token", lambda raw: "digest")
    monkeypatch.setattr(service, "web_account_creation_policy", lambda db: (_ for _ in ()).throw(ValueError("SECRET")))
    request = SimpleNamespace(cookies={service.COOKIE: "COOKIE"})
    with pytest.raises(CredentialAssignmentError) as caught:
        service.create_web_user(
            db, request=request, email="a@b", first_name="A", last_name="B", password="SECRET", role="x"
        )
    assert exited == [True]
    assert "SECRET" not in str(caught.value)
    assert caught.value.__suppress_context__


def test_rollback_latch_never_becomes_expected_product_error(monkeypatch):
    db = BoundarySession()
    monkeypatch.setattr(service, "conflict_savepoint_requires_rollback", lambda *a, **k: True)
    with pytest.raises(CredentialAssignmentError):
        service._require_recovered_root(db, db.root)


@pytest.mark.parametrize("failure", ["inactive", "root", "latch"])
def test_public_expected_refusal_requires_successful_savepoint_recovery(monkeypatch, failure):
    from contextlib import contextmanager

    from dotmac_kernel.exceptions import ForbiddenError

    db = BoundarySession()

    @contextmanager
    def failed_recovery(db):
        try:
            yield
        finally:
            if failure == "inactive":
                db.is_active = False
            elif failure == "root":
                db.root = object()

    monkeypatch.setattr(service, "require_tenant", lambda request: SimpleNamespace(id=uuid4()))
    monkeypatch.setattr(service, "web_account_creation_policy", lambda db: object())
    monkeypatch.setattr(service, "conflict_savepoint", failed_recovery)
    monkeypatch.setattr(service, "conflict_savepoint_requires_rollback", lambda *a, **k: failure == "latch")
    with pytest.raises(CredentialAssignmentError) as caught:
        service.create_web_user(
            db,
            request=SimpleNamespace(cookies={}),
            email="a@b",
            first_name="A",
            last_name="B",
            password="SECRET",
            role="x",
        )
    assert not isinstance(caught.value, ForbiddenError)
    assert str(caught.value) == "account creation requires caller transaction rollback"


@pytest.fixture
def canonical_web_account(request):
    """Use genuine existing Academy fixtures, refusing their optional skips.

    No synthetic schema/SQLite/authority port. This requires hosted successor
    installation, canonical RLS/FKs and the consumed assembly audit action.
    """
    import os

    from dotmac_kernel.models import AuthSession, Party, PartyPerson, PartyRoleGrant, PartyType, Role
    from sqlalchemy import inspect, select, text
    from starlette.requests import Request

    from app.models.identity_extensions import AcademyAccountProfile

    assert os.environ.get("CI") == "true", "canonical adapter canaries are hosted CI only"
    assert os.getenv("TEST_DATABASE_URL"), "canonical PostgreSQL worker DSN is required; skipping is forbidden"
    assert os.getenv("TEST_MIGRATION_DATABASE_URL"), "canonical migration DSN is required"
    # Load the actual product composition/provider graph; never a test registry.
    request.getfixturevalue("app_client")
    tenant = request.getfixturevalue("tenant_a")
    admin = request.getfixturevalue("admin_session")
    db = request.getfixturevalue("app_user_session")
    assert db.get_bind().dialect.name == "postgresql"
    assert not db.execute(text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user")).scalar_one()
    inspector = inspect(admin.get_bind())
    for privilege in ("SELECT", "INSERT", "UPDATE"):
        assert db.execute(
            text("SELECT has_table_privilege(current_user, 'public.roles', :privilege)"), {"privilege": privilege}
        ).scalar_one()
    for table in (
        "roles",
        "parties",
        "party_persons",
        "party_role_grants",
        "auth_sessions",
        "user_credentials",
        "academy_account_profiles",
        "academy_credential_policies",
    ):
        assert inspector.has_table(table, schema="public"), f"canonical prerequisite missing: {table}"
        assert admin.execute(
            text("SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE oid=to_regclass(:table)"),
            {"table": f"public.{table}"},
        ).scalar_one()
    expected_fks = {
        "party_role_grants": {"fk_party_role_grants_tenant_party", "fk_party_role_grants_tenant_role"},
        "auth_sessions": {"fk_auth_sessions_tenant_party"},
        "user_credentials": {"fk_user_credentials_tenant_party"},
        "academy_account_profiles": {"fk_academy_account_tenant_party", "fk_academy_account_person"},
        "academy_credential_policies": {"fk_academy_policy_tenant_credential"},
    }
    for table, names in expected_fks.items():
        assert names.issubset({fk["name"] for fk in inspector.get_foreign_keys(table, schema="public")})
    resolve_database_runtime().set_tenant(db, tenant.id)
    policy = service.web_account_creation_policy(db)
    # The hosted default-policy lane must exercise the actual strict factory.
    assert dict(policy.role_admission) == {
        "student": ("instructor", "admin"),
        "instructor": ("admin",),
        "admin": ("admin",),
    }
    assert policy.minimum_length == 0
    now = datetime.now(UTC)
    roles = {}
    for slug in ("student", "instructor", "admin"):
        row = admin.scalars(select(Role).where(Role.tenant_id == tenant.id, Role.slug == slug)).first()
        if row is None:
            row = Role(tenant_id=tenant.id, slug=slug, name=slug)
            admin.add(row)
            admin.flush()
        roles[slug] = row
    actor = Party(
        tenant_id=tenant.id,
        party_type=PartyType.person,
        display_name="Web Actor",
        email=f"actor-{uuid4().hex}@example.edu",
        is_active=True,
    )
    admin.add(actor)
    admin.flush()
    admin.add(PartyPerson(party_id=actor.id, first_name="Web", last_name="Actor"))
    admin.flush()
    profile = AcademyAccountProfile(
        tenant_id=tenant.id,
        party_id=actor.id,
        status="active",
        avatar_path=None,
        prefs={},
        created_at=now,
        updated_at=now,
    )
    admin.add(profile)
    raw = uuid4().hex + uuid4().hex
    from datetime import timedelta

    session = AuthSession(
        tenant_id=tenant.id, party_id=actor.id, token_hash=service.hash_token(raw), expires_at=now + timedelta(hours=1)
    )
    admin.add(session)
    admin.add(PartyRoleGrant(tenant_id=tenant.id, party_id=actor.id, role_id=roles["admin"].id))
    admin.commit()
    scope = {"type": "http", "headers": [(b"cookie", f"{service.COOKIE}={raw}".encode())], "state": {"tenant": tenant}}
    yield SimpleNamespace(
        db=db,
        admin=admin,
        tenant=tenant,
        actor=actor,
        profile=profile,
        session=session,
        request=Request(scope),
        raw=raw,
        roles=roles,
    )
    db.rollback()


def _create_canonical(account, **overrides):
    values = {
        "request": account.request,
        "email": f"new-{uuid4().hex}@example.edu",
        "first_name": "New",
        "last_name": "Account",
        "password": "x",
        "role": "student",
    }
    values.update(overrides)
    return service.create_web_user(account.db, **values)


def test_canonical_short_supplied_password_one_hash_false_current_audit(canonical_web_account, monkeypatch):
    import dotmac_kernel.credential_lifecycle as owner
    from dotmac_kernel.audit import AuditEvent
    from dotmac_kernel.models import AuthSession, UserCredential
    from dotmac_kernel.security import verify_password
    from sqlalchemy import select

    from app.models.identity_extensions import AcademyCredentialPolicy

    account = canonical_web_account
    calls = []
    audit_refs = []
    real_record = service._WebAssignmentPorts.record_assignment
    real_hash = owner.hash_password

    def hash_once(secret):
        calls.append(True)
        return real_hash(secret)

    def record_current(ports, entry):
        audit_ref = real_record(ports, entry)
        audit_refs.append(audit_ref)
        return audit_ref

    monkeypatch.setattr(service._WebAssignmentPorts, "record_assignment", record_current)
    monkeypatch.setattr(owner, "hash_password", hash_once)
    view = _create_canonical(account)
    credential = account.db.scalars(select(UserCredential).where(UserCredential.party_id == view.id)).one()
    policy = account.db.get(AcademyCredentialPolicy, credential.id)
    assert calls == [True]
    assert verify_password("x", credential.password_hash)
    # This deliberately requires the actual canonical login writer integration;
    # verifying the hash alone would not prove the existing caller can log in.
    from app.services.credential_authentication import sign_in

    # Assignment itself creates no session; login must create the genuine one.
    assert account.db.scalars(select(AuthSession).where(AuthSession.party_id == view.id)).all() == []
    assigned_hash = credential.password_hash
    root = account.db.get_transaction()
    authenticated = sign_in(account.db, tenant_id=account.tenant.id, email=policy.email, password="x")
    assert authenticated is not None and authenticated.account.id == view.id
    assert authenticated.account.tenant_id == account.tenant.id
    assert authenticated.account.email == policy.email
    assert account.db.get_transaction() is root
    assert calls == [True] and credential.password_hash == assigned_hash
    issued = account.db.scalars(
        select(AuthSession).where(
            AuthSession.tenant_id == account.tenant.id,
            AuthSession.id == authenticated.session_id,
            AuthSession.party_id == view.id,
        )
    ).one()
    assert issued.token_hash == service.hash_token(authenticated.session_token)
    assert issued.revoked_at is None
    assert authenticated.session_token not in repr(authenticated)
    assert policy.reset_required is False and policy.lifecycle_version == 0
    assert policy.failed_login_attempts == 0 and policy.locked_until is None
    event = account.db.scalars(
        select(AuditEvent).where(
            AuditEvent.entity_id == str(credential.id), AuditEvent.action == "credential.password.assigned"
        )
    ).one()
    assert audit_refs == [str(event.id)]
    assert event.id is not None and event.actor_type == "user" and event.actor_id == str(account.actor.id)
    assert account.raw not in repr(event.details) and credential.password_hash not in repr(event.details)
    assert account.db.scalars(
        select(AuthSession).where(AuthSession.tenant_id == account.tenant.id, AuthSession.party_id == view.id)
    ).all() == [issued]
    current_session = account.db.get(AuthSession, account.session.id)
    assert current_session.revoked_at is None


@pytest.mark.parametrize("actor_role", [None, "student", "instructor", "admin"])
@pytest.mark.parametrize("target_role", ["student", "instructor", "admin", "unknown"])
def test_canonical_default_admission_and_error_precedence(canonical_web_account, actor_role, target_role):
    from dotmac_kernel.exceptions import BadRequestError, ForbiddenError
    from dotmac_kernel.models import PartyRoleGrant
    from sqlalchemy import delete

    account = canonical_web_account
    account.admin.execute(delete(PartyRoleGrant).where(PartyRoleGrant.party_id == account.actor.id))
    if actor_role is not None:
        account.admin.add(
            PartyRoleGrant(tenant_id=account.tenant.id, party_id=account.actor.id, role_id=account.roles[actor_role].id)
        )
    account.admin.commit()
    if actor_role not in ("instructor", "admin"):
        error = ForbiddenError
    elif target_role == "unknown":
        error = BadRequestError
    elif actor_role == "instructor" and target_role != "student":
        error = ForbiddenError
    else:
        assert _create_canonical(account, role=target_role).id
        return
    with pytest.raises(error):
        _create_canonical(account, role=target_role)


@pytest.mark.parametrize("mutation", ["expired", "revoked", "tampered", "wrongtenant", "suspended", "inactive"])
def test_canonical_refreshed_authority_refuses_stale_state(canonical_web_account, mutation):
    from datetime import timedelta

    from dotmac_kernel.exceptions import ForbiddenError
    from dotmac_kernel.models import AuthSession, Party
    from sqlalchemy import select

    from app.models.identity_extensions import AcademyAccountProfile

    account = canonical_web_account
    # Prime stale ORM rows, then mutate through an independent actual connection.
    cached_session = account.db.scalars(select(AuthSession).where(AuthSession.id == account.session.id)).one()
    cached_party = account.db.scalars(select(Party).where(Party.id == account.actor.id)).one()
    cached_profile = account.db.scalars(
        select(AcademyAccountProfile).where(AcademyAccountProfile.party_id == account.actor.id)
    ).one()
    # SQLAlchemy's identity map is weak: keep these genuine ORM rows alive
    # across the independent commit and the adapter's populate_existing reads.
    cached_rows = (cached_session, cached_party, cached_profile)
    original_state = (
        cached_session.expires_at,
        cached_session.revoked_at,
        cached_party.is_active,
        cached_profile.status,
    )
    assert original_state[0] > datetime.now(UTC)
    assert original_state[1:] == (None, True, "active")
    if mutation == "expired":
        account.session.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif mutation == "revoked":
        account.session.revoked_at = datetime.now(UTC)
    elif mutation == "tampered":
        account.request._cookies = {service.COOKIE: "tampered"}
    elif mutation == "wrongtenant":
        account.request.state.tenant = SimpleNamespace(id=uuid4())
    elif mutation == "suspended":
        account.profile.status = "suspended"
    else:
        account.actor.is_active = False
    intended_state = (
        account.session.expires_at,
        account.session.revoked_at,
        account.actor.is_active,
        account.profile.status,
    )
    account.admin.commit()
    committed_state = (
        account.session.expires_at,
        account.session.revoked_at,
        account.actor.is_active,
        account.profile.status,
    )
    assert committed_state == intended_state
    # Prove the worker still holds the original stale values BEFORE invoking
    # the adapter. Request-cookie/tenant mutations do not mutate database rows.
    assert (
        cached_session.expires_at,
        cached_session.revoked_at,
        cached_party.is_active,
        cached_profile.status,
    ) == original_state
    if mutation == "expired":
        assert cached_session.expires_at != committed_state[0]
        assert committed_state[0] < datetime.now(UTC)
    elif mutation == "revoked":
        assert cached_session.revoked_at is None and committed_state[1] is not None
    elif mutation == "suspended":
        assert cached_profile.status == "active" and committed_state[3] == "suspended"
    elif mutation == "inactive":
        assert cached_party.is_active is True and committed_state[2] is False
    else:
        assert mutation in ("tampered", "wrongtenant") and committed_state == original_state
    email = f"refused-{uuid4().hex}@example.edu"
    with pytest.raises(ForbiddenError):
        _create_canonical(account, email=email)
    # Keep strong references live through refusal without requiring old values:
    # successful populate_existing checks may have refreshed these same objects.
    assert cached_rows == (cached_session, cached_party, cached_profile)
    # Catch followed by normal boundary commit must leave no account effects.
    account.db.commit()
    assert account.admin.scalars(select(Party).where(Party.email == email)).first() is None


@pytest.mark.parametrize("step", ["party", "person", "profile", "grant", "credential", "policy", "audit"])
def test_canonical_failure_after_each_flushed_component_rolls_back(canonical_web_account, step):
    from dotmac_kernel.audit import AuditEvent
    from dotmac_kernel.models import Party, PartyPerson, PartyRoleGrant, UserCredential
    from sqlalchemy import event, select, text

    from app.models.identity_extensions import AcademyAccountProfile, AcademyCredentialPolicy

    account = canonical_web_account
    classes = {
        "party": Party,
        "person": PartyPerson,
        "profile": AcademyAccountProfile,
        "grant": PartyRoleGrant,
        "credential": UserCredential,
        "policy": AcademyCredentialPolicy,
        "audit": AuditEvent,
    }
    selected = classes[step]
    email = f"rollback-{uuid4().hex}@example.edu"

    def fault(mapper, connection, target):
        raise RuntimeError("SECRET COOKIE HASH must be masked")

    event.listen(selected, "after_insert", fault)
    try:
        with pytest.raises(CredentialAssignmentError) as caught:
            _create_canonical(account, email=email, password="SECRET COOKIE HASH")
    finally:
        event.remove(selected, "after_insert", fault)
    assert "SECRET" not in str(caught.value)
    assert caught.value.__suppress_context__
    assert account.db.execute(text("SELECT current_setting('app.current_tenant')")).scalar_one() == str(
        account.tenant.id
    )
    account.db.commit()
    # The savepoint restores the transaction-local tenant before boundary commit.
    # Rebind a new root only after checking the recovered transaction earlier.
    resolve_database_runtime().set_tenant(account.db, account.tenant.id)
    assert account.db.scalars(select(Party).where(Party.email == email)).first() is None
    assert account.admin.scalars(select(Party).where(Party.email == email)).first() is None
    assert account.db.execute(text("SELECT current_setting('app.current_tenant')")).scalar_one() == str(
        account.tenant.id
    )


@pytest.mark.parametrize("writer", ["role", "session", "status"])
@pytest.mark.parametrize("writer_first", [True, False])
def test_canonical_two_connection_principal_first_serialization(canonical_web_account, writer, writer_first):
    """Real lock races, not proof that legacy product writers were converted.

    The explicit canonical writer operation below follows the required common
    Party-first protocol. Adoption additionally must run these races against
    the real converted users_role/revoke_session/set_account_status paths.
    """
    from dotmac_kernel.exceptions import ForbiddenError
    from dotmac_kernel.models import Party, PartyRoleGrant
    from sqlalchemy import delete, select, text
    from sqlalchemy.exc import OperationalError

    account = canonical_web_account
    actor_id, tenant_id = account.actor.id, account.tenant.id
    account.admin.rollback()

    def lock_principal():
        return account.admin.scalars(
            select(Party).where(Party.tenant_id == tenant_id, Party.id == actor_id).with_for_update()
        ).one()

    def change_authority():
        # Actual participating writer rows; the Party lock is acquired first.
        if writer == "role":
            account.admin.execute(
                delete(PartyRoleGrant).where(PartyRoleGrant.tenant_id == tenant_id, PartyRoleGrant.party_id == actor_id)
            )
        elif writer == "session":
            account.session.revoked_at = datetime.now(UTC)
        else:
            account.profile.status = "suspended"
        account.admin.flush()

    email = f"race-{uuid4().hex}@example.edu"
    if writer_first:
        lock_principal()
        change_authority()
        account.db.execute(text("SET LOCAL lock_timeout = '250ms'"))
        with pytest.raises(CredentialAssignmentError):
            _create_canonical(account, email=email)
        # Session is still unrevoked in the worker snapshot until the writer
        # commits; principal-first blocked before taking a session lock.
        account.admin.commit()
        with pytest.raises(ForbiddenError):
            _create_canonical(account, email=email)
        account.db.commit()
        assert account.admin.scalars(select(Party).where(Party.email == email)).first() is None
    else:
        view = _create_canonical(account, email=email)
        account.admin.execute(text("SET LOCAL lock_timeout = '250ms'"))
        with pytest.raises(OperationalError) as caught:
            lock_principal()
        assert getattr(caught.value.orig, "sqlstate", None) == "55P03"
        account.admin.rollback()
        account.db.commit()
        lock_principal()
        change_authority()
        account.admin.commit()
        resolve_database_runtime().set_tenant(account.db, tenant_id)
        assert account.db.get(Party, view.id) is not None


def test_canonical_configured_role_labels_and_short_password(canonical_web_account, monkeypatch):
    """A genuine catalog/grant with a composed settings-policy value, no enum."""
    from dotmac_kernel.models import PartyRoleGrant, Role
    from sqlalchemy import delete

    from app.services.account_creation_policy import AcademyWebAccountCreationPolicy

    account = canonical_web_account
    roles = [Role(tenant_id=account.tenant.id, slug=slug, name=slug) for slug in ("mentor", "learner")]
    account.admin.add_all(roles)
    account.admin.flush()
    account.admin.execute(delete(PartyRoleGrant).where(PartyRoleGrant.party_id == account.actor.id))
    account.admin.add(PartyRoleGrant(tenant_id=account.tenant.id, party_id=account.actor.id, role_id=roles[0].id))
    account.admin.commit()
    policy = AcademyWebAccountCreationPolicy({"learner": ("mentor",)}, 0, {})
    # Settings parsing/readfailures are covered by the policy owner's canaries;
    # this adapter test composes the exact immutable policy with real authority.
    monkeypatch.setattr(service, "web_account_creation_policy", lambda db: policy)
    assert _create_canonical(account, role="learner", password="").id


def test_canonical_provisions_all_configured_roles_and_preserves_existing_names(canonical_web_account, monkeypatch):
    from dotmac_kernel.models import PartyRoleGrant, Role
    from sqlalchemy import delete, select

    from app.services.account_creation_policy import AcademyWebAccountCreationPolicy

    account = canonical_web_account
    student = account.roles["student"]
    student.name = "Tenant custom student name"
    account.admin.execute(delete(Role).where(Role.id == account.roles["instructor"].id))
    account.admin.commit()
    policy = AcademyWebAccountCreationPolicy(
        {"student": ("admin",)},
        0,
        {
            "student": "Student",
            "instructor": "Instructor",
            "admin": "Administrator",
            "observer": "Exact configured observer display name",
        },
    )
    monkeypatch.setattr(service, "web_account_creation_policy", lambda db: policy)
    view = _create_canonical(account)
    catalog = {row.slug: row for row in account.db.scalars(select(Role).where(Role.tenant_id == account.tenant.id))}
    assert catalog["student"].id == student.id and catalog["student"].name == "Tenant custom student name"
    assert catalog["instructor"].name == "Instructor"
    assert catalog["observer"].name == "Exact configured observer display name"
    grants = account.db.scalars(select(PartyRoleGrant).where(PartyRoleGrant.party_id == view.id)).all()
    assert [row.role_id for row in grants] == [student.id]


@pytest.mark.parametrize("provision", [True, False])
def test_canonical_missing_target_requires_explicit_provisioning(canonical_web_account, monkeypatch, provision):
    from dotmac_kernel.models import PartyRoleGrant, Role
    from sqlalchemy import select

    from app.services.account_creation_policy import AcademyWebAccountCreationPolicy

    account = canonical_web_account
    slug = f"new-role-{uuid4().hex}"
    policy = AcademyWebAccountCreationPolicy({slug: ("admin",)}, 0, {slug: "Configured target"} if provision else {})
    monkeypatch.setattr(service, "web_account_creation_policy", lambda db: policy)
    if provision:
        view = _create_canonical(account, role=slug)
        target_role = account.db.scalars(
            select(Role).where(Role.tenant_id == account.tenant.id, Role.slug == slug)
        ).one()
        assert target_role.name == "Configured target"
        grant = account.db.scalars(select(PartyRoleGrant).where(PartyRoleGrant.party_id == view.id)).one()
        assert grant.role_id == target_role.id
    else:
        with pytest.raises(CredentialAssignmentError):
            _create_canonical(account, role=slug)
        assert (
            account.db.scalars(select(Role).where(Role.tenant_id == account.tenant.id, Role.slug == slug)).first()
            is None
        )


@pytest.mark.parametrize("refusal", ["actor", "target", "email", "store"])
def test_canonical_provisioning_stays_in_whole_account_savepoint(canonical_web_account, monkeypatch, refusal):
    from dotmac_kernel.exceptions import BadRequestError, ForbiddenError
    from dotmac_kernel.models import PartyRoleGrant, Role
    from sqlalchemy import delete, select

    from app.services.account_creation_policy import AcademyWebAccountCreationPolicy

    account = canonical_web_account
    added_slug = f"provision-{uuid4().hex}"
    policy = AcademyWebAccountCreationPolicy({"student": ("admin",)}, 0, {added_slug: "Exact added role"})
    monkeypatch.setattr(service, "web_account_creation_policy", lambda db: policy)
    kwargs = {}
    if refusal == "actor":
        account.admin.execute(delete(PartyRoleGrant).where(PartyRoleGrant.party_id == account.actor.id))
        account.admin.commit()
        expected = ForbiddenError
    elif refusal == "target":
        kwargs["role"] = "unknown"
        expected = BadRequestError
    elif refusal == "email":
        kwargs["email"] = account.actor.email
        expected = BadRequestError
    else:

        def fail_store(*args, **kwargs):
            raise RuntimeError("private material")

        monkeypatch.setattr(service._WebAssignmentPorts, "create_absent", fail_store)
        expected = CredentialAssignmentError
    with pytest.raises(expected):
        _create_canonical(account, **kwargs)
    account.db.commit()
    assert (
        account.admin.scalars(select(Role).where(Role.tenant_id == account.tenant.id, Role.slug == added_slug)).first()
        is None
    )


def test_canonical_cookie_expiring_while_waiting_for_principal_refuses(canonical_web_account):
    from datetime import timedelta
    from threading import Thread
    from time import monotonic, sleep

    from dotmac_kernel.exceptions import ForbiddenError
    from dotmac_kernel.models import Party
    from sqlalchemy import select, text

    account = canonical_web_account
    expires = datetime.now(UTC) + timedelta(seconds=1)
    account.session.expires_at = expires
    account.admin.commit()
    actor_id, tenant_id = account.actor.id, account.tenant.id
    account.admin.scalars(
        select(Party).where(Party.id == actor_id, Party.tenant_id == tenant_id).with_for_update()
    ).one()
    # Prefetch request tenant identity before transferring sole Session use to
    # the worker thread. The main thread only operates the other connection.
    assert account.request.state.tenant.id == tenant_id
    worker_pid = account.db.execute(text("SELECT pg_backend_pid()")).scalar_one()
    # The sole holder of the principal's row lock is the admin connection. Blocking
    # is read with pg_blocking_pids, which needs no pg_read_all_stats membership;
    # wait_event_type of another role's backend is hidden from app_admin.
    blocker_pid = account.admin.execute(text("SELECT pg_backend_pid()")).scalar_one()
    email = f"expiry-wait-{uuid4().hex}@example.edu"
    failures = []

    def create_after_wait():
        try:
            _create_canonical(account, email=email)
        except Exception as error:
            failures.append(type(error))

    worker = Thread(target=create_after_wait, daemon=True)
    worker.start()
    try:
        deadline = monotonic() + 5
        while monotonic() < deadline:
            blocked = account.admin.execute(
                text("SELECT :blocker = ANY(pg_blocking_pids(:pid))"), {"blocker": blocker_pid, "pid": worker_pid}
            ).scalar_one()
            if blocked:
                break
            sleep(0.01)
        else:
            pytest.fail("canonical creator did not wait on the locked principal")
        while datetime.now(UTC) <= expires:
            sleep(0.01)
        account.admin.commit()
    finally:
        account.admin.rollback()
        worker.join(timeout=5)
    assert not worker.is_alive(), "canonical creator did not finish after principal release"
    assert failures == [ForbiddenError]
    account.db.commit()
    assert account.admin.scalars(select(Party).where(Party.email == email)).first() is None


def test_canonical_normalized_email_race_one_complete_account(canonical_web_account):
    from threading import Event, Thread
    from time import monotonic, sleep

    from dotmac_kernel.exceptions import BadRequestError
    from dotmac_kernel.models import Party, PartyPerson, PartyRoleGrant, UserCredential
    from sqlalchemy import select, text
    from sqlalchemy.orm import Session

    from app.models.identity_extensions import AcademyAccountProfile, AcademyCredentialPolicy

    account = canonical_web_account
    tenant_id = account.tenant.id
    email = f"race-email-{uuid4().hex}@example.edu"
    winner = _create_canonical(account, email=email)
    ready = Event()
    pids, failures = [], []
    # The winner's uncommitted account rows (account.db) are what the contender waits on.
    blocker_pid = account.db.execute(text("SELECT pg_backend_pid()")).scalar_one()

    def contender():
        # A genuine second worker connection using the existing fixture engine,
        # not a second production Session owner or a SQLite substitute.
        with Session(bind=account.db.get_bind()) as second:
            try:
                resolve_database_runtime().set_tenant(second, tenant_id)
                pids.append(second.execute(text("SELECT pg_backend_pid()")).scalar_one())
                ready.set()
                service.create_web_user(
                    second,
                    request=account.request,
                    email=f"  {email.upper()}  ",
                    first_name="Other",
                    last_name="Account",
                    password="x",
                    role="student",
                )
            except BadRequestError:
                failures.append(BadRequestError)
                second.commit()  # The normal caught-response boundary is safe.
            except Exception as error:
                failures.append(type(error))
                second.rollback()

    worker = Thread(target=contender, daemon=True)
    worker.start()
    try:
        assert ready.wait(timeout=5), "second canonical worker did not start"
        deadline = monotonic() + 5
        while monotonic() < deadline:
            blocked = account.admin.execute(
                text("SELECT :blocker = ANY(pg_blocking_pids(:pid))"), {"blocker": blocker_pid, "pid": pids[0]}
            ).scalar_one()
            if blocked:
                break
            sleep(0.01)
        else:
            pytest.fail("normalized-email contender did not wait on authority locks")
        account.db.commit()
    finally:
        account.db.rollback()
        worker.join(timeout=5)
    assert not worker.is_alive(), "normalized-email contender did not finish after release"
    assert failures == [BadRequestError]
    parties = account.admin.scalars(select(Party).where(Party.tenant_id == tenant_id, Party.email == email)).all()
    assert [row.id for row in parties] == [winner.id]
    assert account.admin.get(PartyPerson, winner.id) is not None
    assert account.admin.get(AcademyAccountProfile, winner.id) is not None
    grants = account.admin.scalars(select(PartyRoleGrant).where(PartyRoleGrant.party_id == winner.id)).all()
    assert len(grants) == 1
    credentials = account.admin.scalars(select(UserCredential).where(UserCredential.party_id == winner.id)).all()
    assert len(credentials) == 1
    assert account.admin.get(AcademyCredentialPolicy, credentials[0].id).reset_required is False


@pytest.mark.parametrize("is_active,status", [(False, "active"), (True, "suspended"), (False, "suspended")])
def test_configured_initial_state_creates_real_credential_but_denies_login(
    canonical_web_account, monkeypatch, is_active, status
):
    from dotmac_kernel.models import Party, UserCredential
    from sqlalchemy import select

    from app.models.identity_extensions import AcademyAccountProfile
    from app.services import settings_store
    from app.services.credential_authentication import sign_in

    a = canonical_web_account
    monkeypatch.setattr(settings_store.settings, "account_initial_is_active", is_active)
    monkeypatch.setattr(settings_store.settings, "account_initial_profile_status", status)
    view = _create_canonical(a)
    party = a.db.get(Party, view.id)
    profile = a.db.get(AcademyAccountProfile, view.id)
    assert party.is_active is is_active and profile.status == status
    assert a.db.scalars(select(UserCredential).where(UserCredential.party_id == view.id)).one()
    assert sign_in(a.db, tenant_id=a.tenant.id, email=party.email, password="x") is None
    a.db.refresh(party)
    a.db.refresh(profile)
    assert party.is_active is is_active and profile.status == status


@pytest.mark.parametrize("stage", ["authority", "snapshot", "audit"])
@pytest.mark.parametrize("field", ["is_active", "status"])
def test_initial_state_tamper_refuses_and_caught_outer_commit_keeps_no_account(
    canonical_web_account, monkeypatch, stage, field
):
    from dotmac_kernel.models import Party
    from sqlalchemy import select

    from app.models.identity_extensions import AcademyAccountProfile
    from app.services import settings_store

    a = canonical_web_account
    monkeypatch.setattr(settings_store.settings, "account_initial_is_active", False)
    monkeypatch.setattr(settings_store.settings, "account_initial_profile_status", "suspended")
    method = {"authority": "authorize_and_lock", "snapshot": "_persisted_snapshot", "audit": "record_assignment"}[stage]
    original = getattr(service._WebAssignmentPorts, method)
    seen = []

    def tamper(ports, *args, **kwargs):
        seen.append(ports)
        if field == "is_active":
            ports.target.is_active = True
        else:
            ports.db.get(AcademyAccountProfile, ports.target.id).status = "active"
        ports.db.flush()
        return original(ports, *args, **kwargs)

    monkeypatch.setattr(service._WebAssignmentPorts, method, tamper)
    email = f"state-tamper-{uuid4().hex}@example.edu"
    with pytest.raises(CredentialAssignmentError):
        _create_canonical(a, email=email)
    assert seen and all(ports._initial_identity_policy is None for ports in seen)
    a.db.commit()
    resolve_database_runtime().set_tenant(a.db, a.tenant.id)
    assert a.db.scalars(select(Party).where(Party.email == email)).first() is None
    assert a.admin.scalars(select(Party).where(Party.email == email)).first() is None


def test_midoperation_settings_change_keeps_pinned_state_next_operation_reads_new(canonical_web_account, monkeypatch):
    from dotmac_kernel.models import Party

    from app.models.identity_extensions import AcademyAccountProfile
    from app.services import settings_store

    a = canonical_web_account
    monkeypatch.setattr(settings_store.settings, "account_initial_is_active", False)
    monkeypatch.setattr(settings_store.settings, "account_initial_profile_status", "suspended")
    original = service._WebAssignmentPorts.authorize_and_lock

    def change_after_creation(ports, command):
        monkeypatch.setattr(settings_store.settings, "account_initial_is_active", True)
        monkeypatch.setattr(settings_store.settings, "account_initial_profile_status", "active")
        return original(ports, command)

    monkeypatch.setattr(service._WebAssignmentPorts, "authorize_and_lock", change_after_creation)
    first = _create_canonical(a)
    second = _create_canonical(a)
    assert a.db.get(Party, first.id).is_active is False
    assert a.db.get(AcademyAccountProfile, first.id).status == "suspended"
    assert a.db.get(Party, second.id).is_active is True
    assert a.db.get(AcademyAccountProfile, second.id).status == "active"


@pytest.mark.parametrize("restriction", ["inactive", "suspended", "revoked", "expired", "role"])
def test_inactive_target_policy_never_weakens_actor_authority(canonical_web_account, monkeypatch, restriction):
    from datetime import timedelta

    from dotmac_kernel.exceptions import ForbiddenError
    from dotmac_kernel.models import PartyRoleGrant
    from sqlalchemy import delete

    from app.services import settings_store

    a = canonical_web_account
    monkeypatch.setattr(settings_store.settings, "account_initial_is_active", False)
    monkeypatch.setattr(settings_store.settings, "account_initial_profile_status", "suspended")
    a.db.commit()
    if restriction == "inactive":
        a.actor.is_active = False
    elif restriction == "suspended":
        a.profile.status = "suspended"
    elif restriction == "revoked":
        a.session.revoked_at = datetime.now(UTC)
    elif restriction == "expired":
        a.session.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    else:
        a.admin.execute(delete(PartyRoleGrant).where(PartyRoleGrant.party_id == a.actor.id))
    a.admin.commit()
    resolve_database_runtime().set_tenant(a.db, a.tenant.id)
    with pytest.raises(ForbiddenError):
        _create_canonical(a)


def test_real_settings_storage_aggregate_keeps_custom_initial_state_and_role_names(canonical_web_account, monkeypatch):
    from dotmac_kernel.audit import AuditEvent
    from dotmac_kernel.models import Party, PartyPerson, PartyRoleGrant, Role, UserCredential
    from sqlalchemy import select

    from app.models.identity_extensions import AcademyAccountProfile, AcademyCredentialPolicy
    from app.services import settings_store

    a = canonical_web_account
    tenant_id = a.tenant.id
    existing_role = a.roles["student"]
    existing_role.name = "Tenant retained learner label"
    a.admin.commit()
    observer = f"observer-{uuid4().hex}"
    monkeypatch.setattr(settings_store.settings, "account_initial_is_active", False)
    monkeypatch.setattr(settings_store.settings, "account_initial_profile_status", "awaiting-review")
    monkeypatch.setattr(
        settings_store.settings,
        "web_account_creation_role_provisioning",
        {"student": "Changed configured default", observer: "Configured observer label"},
    )
    email = f"storage-{uuid4().hex}@example.edu"
    view = _create_canonical(a, email=f"  {email.upper()}  ", first_name="Actual", last_name="Person")
    a.db.expire_all()
    party = a.db.scalars(select(Party).where(Party.tenant_id == tenant_id, Party.id == view.id)).one()
    person = a.db.get(PartyPerson, view.id)
    profile = a.db.get(AcademyAccountProfile, view.id)
    grant = a.db.scalars(
        select(PartyRoleGrant).where(PartyRoleGrant.tenant_id == tenant_id, PartyRoleGrant.party_id == view.id)
    ).one()
    credential = a.db.scalars(
        select(UserCredential).where(UserCredential.tenant_id == tenant_id, UserCredential.party_id == view.id)
    ).one()
    catalog = {row.slug: row for row in a.db.scalars(select(Role).where(Role.tenant_id == tenant_id))}
    assert party.email == email and party.display_name == "Actual Person" and party.is_active is False
    assert party.custom_fields == {} and (person.first_name, person.last_name) == ("Actual", "Person")
    assert profile.tenant_id == tenant_id and profile.party_id == party.id and profile.status == "awaiting-review"
    assert profile.avatar_path is None and profile.prefs == {}
    assert (party.created_at, party.updated_at) == (profile.created_at, profile.updated_at)
    assert grant.party_id == party.id and grant.role_id == existing_role.id
    assert (grant.created_at, grant.updated_at) == (party.created_at, party.updated_at)
    assert catalog["student"].id == existing_role.id and catalog["student"].name == "Tenant retained learner label"
    assert catalog[observer].name == "Configured observer label"
    assert a.db.get(AcademyCredentialPolicy, credential.id).email == email
    events = a.db.scalars(
        select(AuditEvent).where(
            AuditEvent.tenant_id == tenant_id,
            AuditEvent.entity_id == str(credential.id),
            AuditEvent.action == "credential.password.assigned",
        )
    ).all()
    assert len(events) == 1 and events[0].actor_id == str(a.actor.id)


def test_genuine_role_uniqueness_interleaving_keeps_web_conflict_and_prior_work(canonical_web_account, monkeypatch):
    from dotmac_kernel.exceptions import BadRequestError
    from dotmac_kernel.models import Party, Role
    from sqlalchemy import select

    from app.services import settings_store

    a = canonical_web_account
    tid = a.tenant.id
    slug = f"racing-{uuid4().hex}"
    email = f"conflict-{uuid4().hex}@example.edu"
    prior = Role(tenant_id=tid, slug=f"prior-{uuid4().hex}", name="Prior caller work")
    a.db.add(prior)
    a.db.flush()
    prior_id = prior.id
    monkeypatch.setattr(settings_store.settings, "web_account_creation_role_provisioning", {slug: "Configured label"})
    original = service.provision_missing_roles
    winner_id = uuid4()

    def concurrent_insert(db, **values):
        # Genuine independent DB commit between the locked catalog read and
        # this caller's insert. PostgreSQL, not a mocked error, classifies it.
        a.admin.add(Role(id=winner_id, tenant_id=tid, slug=slug, name="Concurrent winner"))
        a.admin.commit()
        return original(db, **values)

    monkeypatch.setattr(service, "provision_missing_roles", concurrent_insert)
    root = a.db.get_transaction()
    with pytest.raises(BadRequestError, match="account creation conflicts"):
        _create_canonical(a, email=email)
    assert a.db.is_active and a.db.get_transaction() is root
    a.db.commit()
    assert a.admin.get(Role, prior_id).name == "Prior caller work"
    winner = a.admin.scalars(select(Role).where(Role.tenant_id == tid, Role.slug == slug)).one()
    assert winner.id == winner_id and winner.name == "Concurrent winner"
    assert a.admin.scalars(select(Party).where(Party.tenant_id == tid, Party.email == email)).all() == []
