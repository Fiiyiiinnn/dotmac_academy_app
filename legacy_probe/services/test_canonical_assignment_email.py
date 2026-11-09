"""Hosted-only canonical blank-email refusal canaries, authored NOT RUN.

The unchanged legacy web form admitted blank strings. Canonical identity now
refuses them atomically; raw legacy parity is deliberately not claimed. Existing
invalid historical rows still fail populated-adoption preflight; no generated
email address or new length/pattern policy is introduced.

The genuine hosted fixture body is copied from current129v2's existing canonical
web assignment tests, renamed only. No fake schema or authority is installed.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from dotmac_kernel.audit import AuditEvent
from dotmac_kernel.exceptions import BadRequestError
from dotmac_kernel.models import AuthSession, Party, PartyPerson, PartyRoleGrant, Role, UserCredential
from dotmac_kernel.session_runtime import resolve_database_runtime
from dotmac_kernel.transactions import conflict_savepoint_requires_rollback
from sqlalchemy import select, text

from app.models.identity_extensions import AcademyAccountProfile, AcademyCredentialPolicy
from app.services import credential_assignment as service


@pytest.fixture
def canonical_email_account(request):
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


def _snapshot(db, tenant_id):
    """Compare actual row identities across every canonical assignment effect."""
    columns = (
        Party.id,
        Role.id,
        PartyRoleGrant.id,
        AuthSession.id,
        UserCredential.id,
        AcademyAccountProfile.party_id,
        AcademyCredentialPolicy.credential_id,
        AuditEvent.id,
    )
    result = {
        column.class_.__name__: frozenset(db.scalars(select(column).where(column.class_.tenant_id == tenant_id)).all())
        for column in columns
    }
    result["PartyPerson"] = frozenset(
        db.scalars(
            select(PartyPerson.party_id)
            .join(Party, Party.id == PartyPerson.party_id)
            .where(Party.tenant_id == tenant_id)
        ).all()
    )
    return result


@pytest.mark.parametrize("email", ["", " ", "\t\r\n", " \u2003\u00a0 "])
def test_blank_normalized_email_refuses_atomically_under_real_admin_authority(
    canonical_email_account, monkeypatch, email
):
    import dotmac_kernel.credential_lifecycle as owner

    account = canonical_email_account
    db, tid = account.db, account.tenant.id
    assert db.scalar(text("SELECT current_user")) == "app_user"
    baseline = _snapshot(db, tid)
    root = db.get_transaction()
    hashes = []
    original = owner.hash_password

    def counted(secret):
        hashes.append(True)
        return original(secret)

    monkeypatch.setattr(owner, "hash_password", counted)
    with pytest.raises(BadRequestError, match="canonical account email is required"):
        service.create_web_user(
            db,
            request=account.request,
            email=email,
            first_name="Blank",
            last_name="Refused",
            password="chosen-password9",
            role="student",
        )
    assert hashes == []
    assert db.is_active and db.get_transaction() is root
    assert not conflict_savepoint_requires_rollback(db, expected_root=root)
    assert db.scalar(text("SELECT current_setting('app.current_tenant')")) == str(tid)
    assert _snapshot(db, tid) == baseline
    # The actual caught-response boundary commits its recovered caller root.
    # Neither it nor an independent readback may reveal any partial account.
    db.flush()
    db.commit()
    db.execute(text("SELECT set_config('app.current_tenant', :tenant, true)"), {"tenant": str(tid)})
    assert _snapshot(db, tid) == baseline
    assert _snapshot(account.admin, tid) == baseline
