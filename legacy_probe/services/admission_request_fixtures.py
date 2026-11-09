"""Genuine canonical admin account/sign-in Requests for hosted Admissions tests."""

from datetime import UTC, datetime
from uuid import uuid4

from dotmac_kernel.models import PartyRoleGrant, Role, UserCredential
from dotmac_kernel.session_runtime import resolve_database_runtime
from sqlalchemy import select
from starlette.requests import Request

from app.models.identity_extensions import AcademyCredentialPolicy
from app.services.credential_authentication import sign_in
from app.services.security import hash_password
from legacy_probe.services.test_canonical_invitation_issuance import _person


def admission_admin_request(db, tenant, *, transport="api"):
    """Setup only: actual principal/subtype/profile/credential/grant and login.

    No commit or rollback; callers retain their existing fixture transaction.
    The returned Request conveys a real cookie HMAC or the same real signed JWT.
    """
    resolve_database_runtime().set_tenant(db, tenant.id)
    person = _person(
        db, tenant_id=tenant.id, email=f"admission-admin-{uuid4().hex}@a.edu", first_name="Admission", last_name="Admin"
    )
    credential = UserCredential(
        tenant_id=tenant.id, party_id=person.id, password_hash=hash_password("chosen-password9")
    )
    db.add(credential)
    db.flush()
    now = datetime.now(UTC)
    db.add(
        AcademyCredentialPolicy(
            tenant_id=tenant.id,
            credential_id=credential.id,
            email=person.email,
            failed_login_attempts=0,
            lifecycle_version=0,
            reset_required=False,
            created_at=now,
            updated_at=now,
        )
    )
    role = db.scalars(select(Role).where(Role.tenant_id == tenant.id, Role.slug == "admin")).first()
    if role is None:
        role = Role(tenant_id=tenant.id, slug="admin", name="Admin")
        db.add(role)
        db.flush()
    db.add(PartyRoleGrant(tenant_id=tenant.id, party_id=person.id, role_id=role.id))
    db.flush()
    login = sign_in(db, tenant_id=tenant.id, email=person.email, password="chosen-password9")
    assert login is not None
    header = (
        (b"cookie", f"session={login.session_token}".encode())
        if transport == "web"
        else (b"authorization", f"Bearer {login.session_token}".encode())
    )
    return Request(
        {
            "type": "http",
            "scheme": "https",
            "server": ("academy.example", 443),
            "path": "/",
            "root_path": "",
            "query_string": b"",
            "headers": [header],
            "state": {"tenant": tenant},
        }
    )
