"""Issue #2 port of legacy test_canonical_api_identity.py.

Disposition: test_api_identity_revoked_session_and_role_loss_reauthorize_live
against app/services/api_identity_operations.py + app/api/deps.py.
Ports test_fixed_commands_reauthorize_actual_request_not_old_projection
(all 5 restrictions x 4 commands, restoring live state between rounds
inside one genesis database). The actual-HTTP cases stay in
legacy_probe: the maintained harness binds the app to the shared test
DSN, not the per-test genesis database.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from dotmac_kernel.audit_actions import active_audit_actions
from dotmac_kernel.models import AuthSession, Party, PartyPerson, PartyRoleGrant, PartyType, Role
from dotmac_kernel.session_runtime import resolve_database_runtime
from fastapi import HTTPException
from sqlalchemy import select, text, update
from starlette.requests import Request

from app.api_identity_declarations import API_IDENTITY_AUDIT_ACTIONS, API_IDENTITY_AUDIT_DECLARATION
from app.models.identity_extensions import AcademyAccountProfile
from app.services import api_identity_operations as commands
from tests.issue2_wave1_fixtures import (  # noqa: F401 - pytest fixtures
    admin_engine,
    admin_session,
    app_client,
    app_user_session,
    canonical_account,
    current_account,
    invitation_account,
    lab_policy_rows,
    lab_worker_engine,
    lab_worker_session,
    no_real_console_spawn,
    policy,
    seed_lab,
    sign_in_account,
    tenant_a,
    tenant_b,
)

assert current_account


@pytest.fixture
def api_admin(current_account):
    from app.assembly import academy_feature

    a = current_account
    assert tuple(API_IDENTITY_AUDIT_DECLARATION.audit_actions) == API_IDENTITY_AUDIT_ACTIONS
    assert set(API_IDENTITY_AUDIT_ACTIONS).issubset(academy_feature.audit_actions)
    registry = active_audit_actions()  # Real installed composition only; never patched.
    for action in API_IDENTITY_AUDIT_ACTIONS:
        registry.require(action)
        assert registry.owner(action) == academy_feature.name

    a.db.rollback()
    role = a.admin.scalar(select(Role).where(Role.tenant_id == a.tid, Role.slug == "admin"))
    if role is None:
        role = Role(tenant_id=a.tid, slug="admin", name="Admin")
        a.admin.add(role)
        a.admin.flush()
    a.admin.add(PartyRoleGrant(tenant_id=a.tid, party_id=a.pid, role_id=role.id))
    a.admin.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert a.db.scalar(text("SELECT current_user")) == "app_user"
    a.admin_role_id = role.id
    return a


def _request(a):
    return Request(
        {"type": "http", "headers": [(b"authorization", f"Bearer {a.token}".encode())], "state": {"tenant": a.tenant}}
    )


def _target(a):
    now = datetime.now(UTC)
    row = Party(
        tenant_id=a.tid,
        party_type=PartyType.person,
        display_name="Target Person",
        email=f"target-{uuid4().hex}@a.edu",
        is_active=True,
        custom_fields={},
    )
    a.admin.add(row)
    a.admin.flush()
    a.admin.add(PartyPerson(party_id=row.id, first_name="Target", last_name="Person"))
    a.admin.flush()
    a.admin.add(
        AcademyAccountProfile(
            tenant_id=row.tenant_id, party_id=row.id, status="active", prefs={}, created_at=now, updated_at=now
        )
    )
    a.admin.commit()
    return row.id


def test_api_identity_revoked_session_and_role_loss_reauthorize_live(api_admin):
    a = api_admin
    pid = _target(a)
    original_expires = a.admin.scalar(select(AuthSession.expires_at).where(AuthSession.id == a.session_id))

    def apply(restriction):
        if restriction == "inactive":
            a.admin.execute(update(Party).where(Party.id == a.pid).values(is_active=False))
        elif restriction == "suspended":
            a.admin.execute(
                update(AcademyAccountProfile).where(AcademyAccountProfile.party_id == a.pid).values(status="suspended")
            )
        elif restriction in {"revoked", "expired"}:
            a.admin.execute(
                update(AuthSession)
                .where(AuthSession.id == a.session_id)
                .values(
                    **(
                        {"revoked_at": datetime.now(UTC)}
                        if restriction == "revoked"
                        else {"expires_at": datetime.now(UTC) - timedelta(seconds=1)}
                    )
                )
            )
        else:  # roleloss
            a.admin.delete(
                a.admin.scalar(
                    select(PartyRoleGrant).where(
                        PartyRoleGrant.party_id == a.pid, PartyRoleGrant.role_id == a.admin_role_id
                    )
                )
            )
        a.admin.commit()

    def restore(restriction):
        if restriction == "inactive":
            a.admin.execute(update(Party).where(Party.id == a.pid).values(is_active=True))
        elif restriction == "suspended":
            a.admin.execute(
                update(AcademyAccountProfile).where(AcademyAccountProfile.party_id == a.pid).values(status="active")
            )
        elif restriction == "revoked":
            a.admin.execute(update(AuthSession).where(AuthSession.id == a.session_id).values(revoked_at=None))
        elif restriction == "expired":
            a.admin.execute(
                update(AuthSession).where(AuthSession.id == a.session_id).values(expires_at=original_expires)
            )
        else:  # roleloss
            a.admin.add(PartyRoleGrant(tenant_id=a.tid, party_id=a.pid, role_id=a.admin_role_id))
        a.admin.commit()

    for restriction in ("inactive", "suspended", "revoked", "expired", "roleloss"):
        apply(restriction)
        for command in ("person", "role", "grant", "delete"):
            request = _request(a)
            with pytest.raises(HTTPException) as caught:
                if command == "person":
                    commands.create_person(
                        a.db, request=request, email=f"denied-{uuid4().hex}@a.edu", first_name="A", last_name="B"
                    )
                elif command == "role":
                    commands.create_role(a.db, request=request, slug="denied-" + uuid4().hex, name="Denied")
                elif command == "grant":
                    commands.grant_role(a.db, request=request, person_id=pid, role_id=a.admin_role_id)
                else:
                    commands.delete_person(a.db, request=request, person_id=pid)
            assert caught.value.status_code == (403 if restriction == "roleloss" else 401), (
                restriction,
                command,
            )
            a.db.rollback()
            resolve_database_runtime().set_tenant(a.db, a.tid)
        restore(restriction)
        a.db.rollback()
        resolve_database_runtime().set_tenant(a.db, a.tid)
    a.db.commit()
