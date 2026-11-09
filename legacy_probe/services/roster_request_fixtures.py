"""Hosted roster actors: canonical credentials and real sign-in sessions."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from dotmac_kernel.models import Party, PartyPerson, PartyRoleGrant, PartyType, Role, UserCredential
from dotmac_kernel.session_runtime import resolve_database_runtime
from sqlalchemy import select

from app.models.identity_extensions import AcademyAccountProfile, AcademyCredentialPolicy
from app.services.credential_authentication import sign_in
from app.services.security import hash_password
from legacy_probe.services.test_canonical_current_user import _request, current_account

assert current_account


def create_roster_actor(a, role_slug):
    """A separate active principal leaves inactive/missing TARGET facts untouched."""
    a.db.rollback()
    now = datetime.now(UTC)
    person = Party(tenant_id=a.tid, party_type=PartyType.person, display_name="Roster Actor",
                   email=f"roster-{uuid4().hex}@a.edu", is_active=True)
    a.admin.add(person)
    a.admin.flush()
    a.admin.add(PartyPerson(party_id=person.id, first_name="Roster", last_name="Actor"))
    a.admin.flush()
    a.admin.add(AcademyAccountProfile(tenant_id=a.tid, party_id=person.id, status="active",
                                     prefs={}, created_at=now, updated_at=now))
    credential = UserCredential(tenant_id=a.tid, party_id=person.id,
                                password_hash=hash_password("chosen-password9"))
    a.admin.add(credential)
    a.admin.flush()
    a.admin.add(AcademyCredentialPolicy(tenant_id=a.tid, credential_id=credential.id, email=person.email,
                                       failed_login_attempts=0, lifecycle_version=0, reset_required=False,
                                       created_at=now, updated_at=now))
    role = a.admin.scalars(select(Role).where(Role.tenant_id == a.tid, Role.slug == role_slug)).first()
    if role is None:
        role = Role(tenant_id=a.tid, slug=role_slug, name=role_slug)
        a.admin.add(role)
        a.admin.flush()
    a.admin.add(PartyRoleGrant(tenant_id=a.tid, party_id=person.id, role_id=role.id))
    a.admin.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    login = sign_in(a.db, tenant_id=a.tid, email=person.email, password="chosen-password9")
    assert login is not None
    a.db.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    return SimpleNamespace(request=_request(a.tenant, login.session_token), pid=person.id,
                           email=person.email, session_id=login.session_id, role_id=role.id)


@pytest.fixture
def roster_actors(current_account):
    a = current_account
    instructor = create_roster_actor(a, "instructor")
    admin = create_roster_actor(a, "admin")
    return SimpleNamespace(db=a.db, instructor=instructor, admin=admin, account=a)
