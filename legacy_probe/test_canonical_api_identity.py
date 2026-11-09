"""Authored hosted-only API/root canaries; installed owner assembly is required.

No action-registry patch, fake principal, metadata.create_all, or local DB lane.
Original API isolation tests are captured separately without assertion changes.
The complete populated installed/disabled-module incoming graph remains a root gate.
"""

from copy import copy
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from dotmac_kernel.audit import AuditEvent
from dotmac_kernel.audit_actions import active_audit_actions
from dotmac_kernel.models import AuthSession, Party, PartyPerson, PartyRoleGrant, PartyType, Role, UserCredential
from dotmac_kernel.session_runtime import resolve_database_runtime
from fastapi import HTTPException
from sqlalchemy import event, insert, inspect, select, text, update
from starlette.requests import Request

from app.api_identity_declarations import API_IDENTITY_AUDIT_ACTIONS, API_IDENTITY_AUDIT_DECLARATION
from app.models.identity_extensions import AcademyAccountProfile, AcademyCredentialPolicy
from app.services import api_identity_operations as commands
from app.services import api_identity_reads as reads
from app.services import audit_context as boundary
from app.services import canonical_identity_store as store
from legacy_probe.conftest import client_for


@pytest.fixture
def api_admin(current_account, app_client):
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
    a.client = client_for(app_client, a.tenant.slug)
    a.headers = {"Authorization": f"Bearer {a.token}"}
    a.admin_role_id = role.id
    return a


def _request(a):
    return Request(
        {"type": "http", "headers": [(b"authorization", f"Bearer {a.token}".encode())], "state": {"tenant": a.tenant}}
    )


def _target(a, *, profile=True, active=True, status="active", email=None, tenant_id=None):
    now = datetime.now(UTC)
    row = Party(
        tenant_id=tenant_id or a.tid,
        party_type=PartyType.person,
        display_name="Target Person",
        email=email or f"target-{uuid4().hex}@a.edu",
        is_active=active,
        custom_fields={},
    )
    a.admin.add(row)
    a.admin.flush()
    a.admin.add(PartyPerson(party_id=row.id, first_name="Target", last_name="Person"))
    a.admin.flush()
    if profile:
        a.admin.add(
            AcademyAccountProfile(
                tenant_id=row.tenant_id, party_id=row.id, status=status, prefs={}, created_at=now, updated_at=now
            )
        )
    a.admin.commit()
    return row.id


def _create(a, email=None):
    response = a.client.post(
        "/people",
        headers=a.headers,
        json={"email": email or f"new-{uuid4().hex}@a.edu", "first_name": "  First", "last_name": "Last  "},
    )
    assert response.status_code == 201, response.text
    assert set(response.json()) == {"id", "email", "first_name", "last_name"}
    return response.json()


def test_actual_http_create_exact_fields_policy_no_credentials_or_roles(api_admin):
    a = api_admin
    result = _create(a)
    pid = UUID(result["id"])
    assert result["first_name"] == "  First" and result["last_name"] == "Last  "
    assert reads.get_person(a.db, tenant_id=a.tid, person_id=pid).id == pid
    party, person, profile = a.db.get(Party, pid), a.db.get(PartyPerson, pid), a.db.get(AcademyAccountProfile, pid)
    assert party.id == person.party_id == profile.party_id == pid
    assert party.custom_fields == {} and profile.prefs == {} and profile.avatar_path is None
    for model in (UserCredential, AuthSession, PartyRoleGrant):
        assert a.db.scalar(select(model.id).where(model.tenant_id == a.tid, model.party_id == pid)) is None
    audits = a.db.scalars(
        select(AuditEvent).where(AuditEvent.action == "person.create", AuditEvent.entity_id == str(pid))
    ).all()
    assert len(audits) == 1
    assert audits[0].actor_id == str(a.pid) and audits[0].actor_party_id == a.pid
    assert audits[0].source_application == "dotmac_academy_app" and audits[0].details == {"email": result["email"]}


@pytest.mark.parametrize(
    "profile,active,status", [(True, False, "active"), (True, True, "suspended"), (False, True, "active")]
)
def test_inventory_target_eligibility_is_profile_presence_not_status(api_admin, profile, active, status):
    a = api_admin
    pid = _target(a, profile=profile, active=active, status=status)
    get = a.client.get(f"/people/{pid}", headers=a.headers)
    assert get.status_code == (200 if profile else 404)
    listing = a.client.get("/people", headers=a.headers)
    assert listing.status_code == 200
    assert (str(pid) in {row["id"] for row in listing.json()}) is profile
    grant = a.client.post(
        "/rbac/role-grants", headers=a.headers, json={"person_id": str(pid), "role_id": str(a.admin_role_id)}
    )
    assert grant.status_code == (204 if profile else 404)
    delete = a.client.delete(f"/people/{pid}", headers=a.headers)
    assert delete.status_code == (204 if profile else 404)
    assert delete.content == (b"" if profile else delete.content)


@pytest.mark.parametrize("variant,expected", [("exact", 409), ("case", 409), ("space", 201), ("cross", 201)])
def test_api_email_exact_discovery_and_canonical_lower_no_trim_index(api_admin, tenant_b, variant, expected):
    a = api_admin
    email = f"collision-{uuid4().hex}@a.edu"
    stored = email.upper() if variant == "case" else f" {email} " if variant == "space" else email
    _target(a, email=stored, tenant_id=tenant_b.id if variant == "cross" else a.tid)
    response = a.client.post(
        "/people", headers=a.headers, json={"email": email, "first_name": "First", "last_name": "Last"}
    )
    assert response.status_code == expected, response.text
    if expected == 409:
        body = response.json()
        assert body["code"] == "conflict" and body["message"] == "Email already registered"
    assert a.admin.scalar(select(Party.email).where(Party.email == stored)) == stored


def test_roles_exact_fields_duplicate_nonidempotence_and_grant_duplicate(api_admin):
    a = api_admin
    slug = "role-" + uuid4().hex
    response = a.client.post("/rbac/roles", headers=a.headers, json={"slug": slug, "name": " Exact Name "})
    assert response.status_code == 201, response.text
    role = response.json()
    assert set(role) == {"id", "slug", "name"} and role["name"] == " Exact Name "
    duplicate = a.client.post("/rbac/roles", headers=a.headers, json={"slug": slug, "name": "Other"})
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "conflict" and duplicate.json()["message"] == "Role already exists"
    pid = _target(a, active=False, status="suspended")
    body = {"person_id": str(pid), "role_id": role["id"]}
    assert a.client.post("/rbac/role-grants", headers=a.headers, json=body).status_code == 204
    duplicate = a.client.post("/rbac/role-grants", headers=a.headers, json=body)
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "conflict" and duplicate.json()["message"] == "Role already assigned"
    assert (
        a.client.post("/rbac/role-grants", headers=a.headers, json={**body, "role_id": str(uuid4())}).status_code == 404
    )


@pytest.mark.parametrize(
    "slug,name,expected",
    [
        ("a", "A", 201),
        ("a" * 63, "N" * 120, 201),
        ("a" * 64, "N", 422),
        ("A", "N", 422),
        ("-a", "N", 422),
        ("a", "", 422),
        ("a", "N" * 121, 422),
    ],
)
def test_role_payload_validation_byte_exact(api_admin, slug, name, expected):
    response = api_admin.client.post("/rbac/roles", headers=api_admin.headers, json={"slug": slug, "name": name})
    assert response.status_code == expected, response.text


def test_nonself_delete_surviving_actor_and_missing_target(api_admin):
    a = api_admin
    pid = _target(a)
    response = a.client.delete(f"/people/{pid}", headers=a.headers)
    assert response.status_code == 204 and response.content == b""
    assert a.client.get("/people", headers=a.headers).status_code == 200
    missing = a.client.delete(f"/people/{pid}", headers=a.headers)
    assert missing.status_code == 404
    assert missing.json()["code"] == "not_found" and missing.json()["message"] == "Person not found"
    event_row = a.db.scalar(
        select(AuditEvent).where(AuditEvent.action == "person.delete", AuditEvent.entity_id == str(pid))
    )
    assert event_row.id is not None and event_row.actor_party_id == a.pid


def test_self_delete_actual_http_keeps_audit_and_invalidates_auth(api_admin):
    a = api_admin
    response = a.client.delete(f"/people/{a.pid}", headers=a.headers)
    assert response.status_code == 204 and response.content == b""
    assert a.client.get("/people", headers=a.headers).status_code == 401
    a.db.expire_all()
    for _model, key in (
        (Party, Party.id),
        (PartyPerson, PartyPerson.party_id),
        (AcademyAccountProfile, AcademyAccountProfile.party_id),
        (AuthSession, AuthSession.party_id),
        (PartyRoleGrant, PartyRoleGrant.party_id),
        (UserCredential, UserCredential.party_id),
    ):
        assert a.db.scalar(select(key).where(key == a.pid)) is None
    audit = a.db.scalar(
        select(AuditEvent).where(AuditEvent.action == "person.delete", AuditEvent.entity_id == str(a.pid))
    )
    assert audit.id is not None and audit.actor_id == str(a.pid) and audit.actor_party_id == a.pid
    assert (
        a.db.scalar(
            select(AcademyCredentialPolicy.credential_id).where(
                AcademyCredentialPolicy.credential_id == a.credential_id
            )
        )
        is None
    )


@pytest.mark.parametrize("command", ["person", "role", "grant", "delete"])
@pytest.mark.parametrize("restriction", ["inactive", "suspended", "revoked", "expired", "roleloss"])
def test_fixed_commands_reauthorize_actual_request_not_old_projection(api_admin, command, restriction):
    a = api_admin
    pid = _target(a)
    request = _request(a)
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
    else:
        a.admin.delete(
            a.admin.scalar(
                select(PartyRoleGrant).where(
                    PartyRoleGrant.party_id == a.pid, PartyRoleGrant.role_id == a.admin_role_id
                )
            )
        )
    a.admin.commit()
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
    assert caught.value.status_code == (403 if restriction == "roleloss" else 401)
    a.db.commit()


@pytest.mark.parametrize(
    "operation,stage",
    [
        ("person", "parties"),
        ("person", "party_persons"),
        ("person", "academy_account_profiles"),
        ("role", "roles"),
        ("grant", "party_role_grants"),
        ("delete", "parties"),
        ("person", "audit_events"),
        ("role", "audit_events"),
        ("grant", "audit_events"),
        ("delete", "audit_events"),
    ],
)
def test_fault_each_core_audit_delete_stage_caught_outer_commit_preserves_prior_work(api_admin, operation, stage):
    a = api_admin
    pid = _target(a)
    root = a.db.get_transaction()
    prior = a.db.get(Role, a.admin_role_id)
    prior.name = "Unrelated prior work"
    email = f"failed-{uuid4().hex}@a.edu"
    slug = "failed-" + uuid4().hex
    fired = False

    def fail(conn, cursor, statement, parameters, context, executemany):
        nonlocal fired
        sql = statement.lower().replace('"', "").replace("public.", "")
        verb = "delete from" if operation == "delete" and stage == "parties" else "insert into"
        if not fired and f"{verb} {stage} " in sql:
            fired = True
            raise RuntimeError("injected owned stage failure")

    event.listen(a.db.get_bind(), "before_cursor_execute", fail)
    try:
        with pytest.raises(boundary.HumanOperationError):
            if operation == "person":
                commands.create_person(a.db, request=_request(a), email=email, first_name="A", last_name="B")
            elif operation == "role":
                commands.create_role(a.db, request=_request(a), slug=slug, name="Failed")
            elif operation == "grant":
                commands.grant_role(a.db, request=_request(a), person_id=pid, role_id=a.admin_role_id)
            else:
                commands.delete_person(a.db, request=_request(a), person_id=pid)
    finally:
        event.remove(a.db.get_bind(), "before_cursor_execute", fail)
    assert fired and a.db.get_transaction() is root
    assert a.db.scalar(text("SELECT current_setting('app.current_tenant')")) == str(a.tid)
    a.db.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert a.db.get(Role, a.admin_role_id).name == "Unrelated prior work"
    assert a.db.get(Party, pid) is not None
    assert a.db.scalar(select(Party.id).where(Party.tenant_id == a.tid, Party.email == email)) is None
    assert a.db.scalar(select(Role.id).where(Role.tenant_id == a.tid, Role.slug == slug)) is None
    assert a.db.scalar(select(PartyRoleGrant.id).where(PartyRoleGrant.party_id == pid)) is None
    assert a.db.scalar(select(AuditEvent.id).where(AuditEvent.tenant_id == a.tid)) is None


@pytest.mark.parametrize("tamper", ["missing", "changed", "different_uuid"])
def test_created_person_exact_private_outcome_cannot_be_swapped(api_admin, tamper):
    a = api_admin
    email = f"outcome-{uuid4().hex}@a.edu"
    with pytest.raises(boundary.HumanOperationError):
        with boundary.create_api_person(
            a.db, request=_request(a), email=email, first_name="A", last_name="B"
        ) as context:
            result = context.create_person()
            if tamper == "missing":
                store.delete_person(a.db, tenant_id=a.tid, party_id=result.id)
            elif tamper == "changed":
                a.db.get(PartyPerson, result.id).first_name = "Changed"
            else:
                # Same fields, a different private generated UUID: no adoption.
                values = context._created_person[0]
                from dataclasses import replace

                store.delete_person(a.db, tenant_id=a.tid, party_id=result.id)
                store.create_person(a.db, values=replace(values, id=uuid4()))
    a.db.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert a.db.scalar(select(Party.id).where(Party.email == email)) is None


@pytest.mark.parametrize("fault", ["omit_delete", "wrong_target", "missing_audit", "completion"])
def test_delete_command_failure_cannot_commit_audit_or_partial_delete(api_admin, monkeypatch, fault):
    a = api_admin
    pid, other = _target(a), _target(a)
    original = store.delete_person
    if fault == "omit_delete":
        monkeypatch.setattr(store, "delete_person", lambda *args, **kwargs: None)
    elif fault == "wrong_target":

        def wrong(db, *, tenant_id, party_id):
            original(db, tenant_id=tenant_id, party_id=other)

        monkeypatch.setattr(store, "delete_person", wrong)
    elif fault == "missing_audit":

        def omit(self, **kwargs):
            return None

        monkeypatch.setattr(boundary._Context, "audit", omit)
    else:

        def refuse(self):
            raise RuntimeError("injected completion failure")

        monkeypatch.setattr(boundary._Context, "complete", refuse)
    with pytest.raises(boundary.HumanOperationError):
        commands.delete_person(a.db, request=_request(a), person_id=pid)
    a.db.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert a.db.get(Party, pid) is not None and a.db.get(Party, other) is not None
    assert a.db.scalar(select(AuditEvent.id).where(AuditEvent.action == "person.delete")) is None


def test_delete_same_uuid_reinsertion_and_copied_context_refuse(api_admin):
    a = api_admin
    pid = _target(a)
    with pytest.raises(boundary.HumanOperationError):
        with boundary.delete_api_person(a.db, request=_request(a), person_id=pid) as context:
            context.delete_person()
            a.db.add(
                Party(
                    id=pid,
                    tenant_id=a.tid,
                    party_type=PartyType.person,
                    email=f"reinsert-{uuid4().hex}@a.edu",
                    display_name="Reinserted",
                    is_active=True,
                )
            )
            a.db.flush()
    a.db.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert a.db.get(Party, pid) is not None
    with pytest.raises(boundary.HumanOperationError):
        with boundary.delete_api_person(a.db, request=_request(a), person_id=pid) as context:
            copied = copy(context)
            copied.delete_person()
    a.db.commit()


def test_public_audit_exact_seven_fields_and_raw_historical_details(api_admin):
    a = api_admin
    result = _create(a)
    produced = a.db.scalar(select(AuditEvent).where(AuditEvent.entity_id == result["id"]))
    # Plant the historical row by INSERT: the runtime role cannot UPDATE audit_events.
    table = AuditEvent.__table__
    mapper = inspect(AuditEvent)
    values = {column.name: getattr(produced, mapper.get_property_by_column(column).key) for column in table.columns}
    values.update(id=uuid4(), details=["historical", None], created_at=None)
    a.db.execute(insert(table).values(**values))
    a.db.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)  # the tenant GUC is transaction-local
    audit = a.db.scalar(select(AuditEvent).where(AuditEvent.id == values["id"]))
    response = a.client.get("/rbac/audit-events", headers=a.headers)
    assert response.status_code == 200, response.text
    row = next(row for row in response.json() if row["id"] == str(audit.id))
    assert set(row) == {"id", "actor_person_id", "action", "entity_type", "entity_id", "details", "created_at"}
    assert row["actor_person_id"] == str(a.pid) and row["details"] == ["historical", None]
    assert row["created_at"] is None


@pytest.mark.parametrize("late_case", [False, True])
def test_late_exact_or_index_collision_rolls_back_without_late_adoption(api_admin, monkeypatch, late_case):
    a = api_admin
    email = f"late-{uuid4().hex}@a.edu"
    original = boundary.initial_identity_policy
    calls = 0
    collided = []

    def after_read(db):
        nonlocal calls
        calls += 1
        result = original(db)
        collided.append(_target(a, email=email.upper() if late_case else email))
        return result

    monkeypatch.setattr(boundary, "initial_identity_policy", after_read)
    with pytest.raises(HTTPException) as caught:
        commands.create_person(a.db, request=_request(a), email=email, first_name="A", last_name="B")
    assert caught.value.status_code == 409 and caught.value.detail == "Email already registered"
    assert calls == 1
    a.db.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    rows = a.db.scalars(select(Party).where(Party.email.in_((email, email.upper())))).all()
    assert len(rows) == 1 and rows[0].id == collided[0] and rows[0].display_name == "Target Person"
    assert a.db.scalar(select(AuditEvent.id).where(AuditEvent.action == "person.create")) is None


@pytest.mark.parametrize("restriction", ["profile", "role", "session"])
def test_natural_secondary_lock_wait_refuses_fresh_actor_changes(api_admin, restriction):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from sqlalchemy.orm import sessionmaker

    from legacy_probe.services.test_canonical_self_profile_locking import _wait_for_lock

    a = api_admin
    a.db.commit()
    sessions = sessionmaker(bind=a.db.get_bind(), autoflush=False)
    ready, backend = Event(), []
    # Hold a genuine secondary row before contender takes its actor Party lock.
    if restriction == "profile":
        row = a.admin.scalar(
            select(AcademyAccountProfile).where(AcademyAccountProfile.party_id == a.pid).with_for_update()
        )
    elif restriction == "role":
        row = a.admin.scalar(select(Role).where(Role.id == a.admin_role_id).with_for_update())
    else:
        row = a.admin.scalar(select(AuthSession).where(AuthSession.id == a.session_id).with_for_update())

    def contender():
        with sessions() as db:
            resolve_database_runtime().set_tenant(db, a.tid)
            backend.append(db.scalar(text("SELECT pg_backend_pid()")))
            ready.set()
            with pytest.raises(HTTPException) as caught:
                commands.create_role(db, request=_request(a), slug="wait-" + uuid4().hex, name="Refused")
            assert caught.value.status_code == (403 if restriction == "role" else 401)
            db.commit()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(contender)
        assert ready.wait(10)
        try:
            resolve_database_runtime().set_tenant(a.db, a.tid)
            _wait_for_lock(a.db, backend[0])
            if restriction == "profile":
                row.status = "suspended"
            elif restriction == "role":
                row.slug = "no-longer-admin"
            else:
                row.revoked_at = datetime.now(UTC)
            a.admin.commit()
        finally:
            a.admin.rollback()
        future.result(timeout=20)


@pytest.mark.parametrize(
    "field,value,valid",
    [
        ("account_initial_is_active", "false", True),
        ("account_initial_profile_status", "custom-status", True),
        ("account_initial_is_active", None, False),
        ("account_initial_is_active", "not-a-bool", False),
        ("account_initial_profile_status", None, False),
        ("account_initial_profile_status", "x" * 21, False),
    ],
)
def test_actual_settings_initial_policy_snapshot_variants(api_admin, field, value, valid):
    from app.models.platform_settings import PlatformSetting

    a = api_admin
    existing = a.admin.get(PlatformSetting, field)
    original_value = existing.value if existing is not None else None
    was_present = existing is not None
    if existing is None:
        existing = PlatformSetting(key=field, value=value)
        a.admin.add(existing)
    else:
        existing.value = value
    a.admin.commit()
    email = f"policy-{uuid4().hex}@a.edu"
    try:
        if valid:
            result = commands.create_person(a.db, request=_request(a), email=email, first_name="A", last_name="B")
            if field == "account_initial_is_active":
                assert a.db.get(Party, result.id).is_active is False
            else:
                assert a.db.get(AcademyAccountProfile, result.id).status == "custom-status"
        else:
            with pytest.raises(boundary.HumanOperationError):
                commands.create_person(a.db, request=_request(a), email=email, first_name="A", last_name="B")
            a.db.commit()
            resolve_database_runtime().set_tenant(a.db, a.tid)
            assert a.db.scalar(select(Party.id).where(Party.email == email)) is None
    finally:
        a.db.commit()
        if was_present:
            existing.value = original_value
        else:
            a.admin.delete(existing)
        a.admin.commit()


def test_long_actor_label_is_omitted_without_changing_genuine_actor(api_admin):
    a = api_admin
    a.admin.execute(update(Party).where(Party.id == a.pid).values(email="a" * 170 + "@a.edu"))
    a.admin.commit()
    role = commands.create_role(a.db, request=_request(a), slug="long-" + uuid4().hex, name="Long actor label")
    audit = a.db.scalar(select(AuditEvent).where(AuditEvent.entity_id == str(role.id)))
    assert audit.actor_label is None and audit.actor_id == str(a.pid) and audit.actor_party_id == a.pid


@pytest.mark.parametrize("self_delete", [False, True])
def test_populated_installed_pg_cascade_and_fkless_retention_requires_real_ordering(api_admin, self_delete):
    """Qualification canary, not a claim that AuthSession RESTRICT ordering works.

    Uses actual installed owner models; one Party delete, no dependent cleanup.
    Additional incoming references from installed/disabled modules remain a root
    registry census gate even when this populated graph passes on PostgreSQL.
    """
    from dotmac_kernel.models import ExternalIdentityBinding
    from dotmac_kernel.settings_models import DomainSettingHistory, SettingChangeAction

    from app.models.account_token import AccountToken
    from app.models.admissions import Applicant
    from app.models.announcement import Announcement
    from app.models.assessment import Activity, Submission
    from app.models.attempt import ActivityAttempt, AttemptGrant
    from app.models.certificate import Certificate
    from app.models.cohort import Cohort, Enrollment
    from app.models.completion import CourseCompletion
    from app.models.course import Chapter, Course
    from app.models.lab import LabInstance
    from app.models.learning_event import LearningEvent
    from app.models.notification import Notification
    from app.models.reading import ChapterRead, SubtopicRead
    from app.models.reminder import ReminderLog, ReminderPreference
    from app.models.success_queue import SuccessQueueEntry
    from app.services.security import hash_password, hash_token

    a = api_admin
    pid = a.pid if self_delete else _target(a, active=False, status="suspended")
    now = datetime.now(UTC)
    credential = (
        a.admin.get(UserCredential, a.credential_id)
        if self_delete
        else UserCredential(tenant_id=a.tid, party_id=pid, password_hash=hash_password("canary-password9"))
    )
    a.admin.add(credential)
    a.admin.flush()
    if not self_delete:
        a.admin.add(
            AcademyCredentialPolicy(
                tenant_id=a.tid,
                credential_id=credential.id,
                email=a.admin.get(Party, pid).email,
                failed_login_attempts=0,
                lifecycle_version=0,
                reset_required=False,
                created_at=now,
                updated_at=now,
            )
        )
        a.admin.add(PartyRoleGrant(tenant_id=a.tid, party_id=pid, role_id=a.admin_role_id))
    binding = ExternalIdentityBinding(
        tenant_id=a.tid,
        party_id=pid,
        provider_binding="hosted-canary",
        issuer="https://identity.example.test",
        subject=uuid4().hex,
        bound_by="hosted-fixture",
        bind_reason="populated cascade qualification",
    )
    a.admin.add(binding)
    a.admin.flush()
    bound_session = AuthSession(
        tenant_id=a.tid,
        party_id=pid,
        token_hash=hash_token(uuid4().hex),
        expires_at=now + timedelta(hours=1),
        external_identity_binding_id=binding.id,
    )
    cohort = Cohort(tenant_id=a.tid, name="Cascade cohort", discipline="fiber", status="active")
    course = Course(
        tenant_id=a.tid,
        slug="cascade-" + uuid4().hex,
        title="Cascade course",
        discipline="fiber",
        source_ref="hosted-canary",
        status="published", listed=False,
    )
    a.admin.add_all([cohort, course, bound_session])
    a.admin.flush()
    chapter = Chapter(tenant_id=a.tid, course_id=course.id, number=1, title="Retained chapter")
    activity = Activity(tenant_id=a.tid, course_id=course.id, type="mcq_test", title="Retained activity", grading="auto", weight=1.0)
    a.admin.add_all([chapter, activity])
    a.admin.flush()
    cascades = [
        bound_session,
        binding,
        credential,
        AccountToken(
            tenant_id=a.tid,
            person_id=pid,
            kind="ical_feed",
            token_hash=hash_token(uuid4().hex),
            expires_at=now + timedelta(hours=1),
        ),
        Enrollment(tenant_id=a.tid, cohort_id=cohort.id, person_id=pid, role_in_cohort="student", status="active"),
        Notification(tenant_id=a.tid, person_id=pid, kind="course_assigned", title="Cascade notification"),
        Announcement(
            tenant_id=a.tid,
            author_person_id=pid,
            cohort_id=cohort.id,
            title="Authored",
            body_md="Body",
            body_html="<p>Body</p>",
        ),
        ReminderLog(
            tenant_id=a.tid, person_id=pid, event_kind="course_assigned", occurrence_key=uuid4().hex, title="Reminder"
        ),
        ReminderPreference(tenant_id=a.tid, person_id=pid, frequency="immediate", optouts=[]),
        SuccessQueueEntry(
            tenant_id=a.tid,
            person_id=pid,
            cohort_id=cohort.id,
            reason_kind="inactivity",
            supporting_facts={},
            severity="high",
            detected_at=now,
        ),
        LearningEvent(tenant_id=a.tid, person_id=pid, kind="course_viewed", detail={}, occurred_at=now),
    ]
    retained = [
        Applicant(
            tenant_id=a.tid,
            email=f"retained-{uuid4().hex}@a.edu",
            first_name="Retained",
            last_name="Applicant",
            person_id=pid,
        ),
        CourseCompletion(
            tenant_id=a.tid, person_id=pid, course_id=course.id, pct=1.0, status="completed", completed_at=now
        ),
        Certificate(tenant_id=a.tid, person_id=pid, course_id=course.id, serial=uuid4().hex, issued_at=now),
        Submission(tenant_id=a.tid, person_id=pid, activity_id=activity.id, answers={}, attempt_no=1),
        ActivityAttempt(tenant_id=a.tid, person_id=pid, activity_id=activity.id, question_ext_ids=[], started_at=now),
        AttemptGrant(
            tenant_id=a.tid,
            person_id=pid,
            activity_id=activity.id,
            extra_attempts=1,
            reason="Retain grantor",
            granted_by=pid,
        ),
        ChapterRead(tenant_id=a.tid, person_id=pid, chapter_id=chapter.id),
        SubtopicRead(tenant_id=a.tid, person_id=pid, chapter_id=chapter.id, subtopic_slug="retained"),
        LabInstance(
            tenant_id=a.tid, person_id=pid, activity_id=activity.id, instance_name="retained-" + uuid4().hex, seed={}
        ),
    ]
    history = DomainSettingHistory(
        tenant_id=a.tid,
        domain="academy",
        key="canary",
        action=SettingChangeAction.update,
        changed_by_party_id=pid,
        value_before="before",
        value_after="after",
        secret_changed=False,
    )
    a.admin.add_all([*cascades, *retained, history])
    a.admin.flush()
    cascade_ids = [(type(row), row.id) for row in cascades]
    retained_ids = [(type(row), row.id) for row in retained]
    history_id, credential_id = history.id, credential.id
    a.admin.commit()
    dml = []

    def observe(conn, cursor, statement, parameters, context, executemany):
        sql = statement.lower().replace('"', "").replace("public.", "")
        if sql.lstrip().startswith(("insert", "delete", "update")):
            dml.append(sql)

    event.listen(a.db.get_bind(), "before_cursor_execute", observe)
    try:
        commands.delete_person(a.db, request=_request(a), person_id=pid)
        a.db.commit()
    finally:
        event.remove(a.db.get_bind(), "before_cursor_execute", observe)
    assert len([sql for sql in dml if sql.lstrip().startswith("delete")]) == 1
    assert next(i for i, sql in enumerate(dml) if sql.startswith("insert into audit_events")) < next(
        i for i, sql in enumerate(dml) if sql.startswith("delete from parties")
    )
    a.admin.expire_all()
    assert a.admin.get(Party, pid) is None and a.admin.get(PartyPerson, pid) is None
    assert a.admin.get(AcademyAccountProfile, pid) is None
    assert a.admin.get(AcademyCredentialPolicy, credential_id) is None
    for model, key in cascade_ids:
        assert a.admin.get(model, key) is None
    for model, key in retained_ids:
        assert a.admin.get(model, key).person_id == pid
    assert a.admin.get(DomainSettingHistory, history_id).changed_by_party_id is None
    assert (
        a.admin.scalar(
            select(AuditEvent.id).where(AuditEvent.action == "person.delete", AuditEvent.entity_id == str(pid))
        )
        is not None
    )
    assert a.client.get("/people", headers=a.headers).status_code == (401 if self_delete else 200)


@pytest.mark.parametrize("operation", ["person", "role", "grant", "delete"])
def test_fixed_audit_receipt_uuid_tamper_latches_rollback(api_admin, operation):
    a = api_admin
    pid = _target(a)
    if operation == "person":
        issued = boundary.create_api_person(
            a.db, request=_request(a), email=f"receipt-{uuid4().hex}@a.edu", first_name="A", last_name="B"
        )
    elif operation == "role":
        issued = boundary.create_api_role(a.db, request=_request(a), slug="receipt-" + uuid4().hex, name="Receipt")
    elif operation == "grant":
        issued = boundary.grant_api_role(a.db, request=_request(a), person_id=pid, role_id=a.admin_role_id)
    else:
        issued = boundary.delete_api_person(a.db, request=_request(a), person_id=pid)
    with pytest.raises(boundary.HumanOperationError):
        with issued as context:
            if operation == "person":
                context.create_person()
            elif operation == "role":
                context.create_role()
            elif operation == "grant":
                context.grant_role()
            else:
                context.delete_person()
            row = context.audits[0][0]
            row.id = uuid4()  # Same fields are insufficient: pin actual writer-returned UUID.
            a.db.flush()
            with pytest.raises(boundary.HumanOperationError):
                context.revalidate()
    a.db.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert a.db.get(Party, pid) is not None
    assert a.db.scalar(select(AuditEvent.id).where(AuditEvent.tenant_id == a.tid)) is None


@pytest.mark.parametrize("operation", ["person", "role", "grant", "delete"])
def test_no_fixed_command_handoff_cannot_complete_by_manually_appending_audit(api_admin, operation):
    a = api_admin
    pid = _target(a)
    if operation == "person":
        issued = boundary.create_api_person(
            a.db, request=_request(a), email=f"omitted-{uuid4().hex}@a.edu", first_name="A", last_name="B"
        )
    elif operation == "role":
        issued = boundary.create_api_role(a.db, request=_request(a), slug="omitted-" + uuid4().hex, name="Omitted")
    elif operation == "grant":
        issued = boundary.grant_api_role(a.db, request=_request(a), person_id=pid, role_id=a.admin_role_id)
    else:
        issued = boundary.delete_api_person(a.db, request=_request(a), person_id=pid)
    with pytest.raises(boundary.HumanOperationError):
        with issued as context:
            if operation == "delete":
                target = reads.get_person(a.db, tenant_id=a.tid, person_id=pid)
                context.audit(details={"email": target.email})
            # Creation cannot audit without its actual generated outcome; deletion
            # cannot complete merely because a caller wrote the expected audit.
    a.db.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert a.db.get(Party, pid) is not None
    assert a.db.scalar(select(AuditEvent.id).where(AuditEvent.tenant_id == a.tid)) is None


def test_owned_new_command_failure_frames_release_actual_request_and_context(api_admin, monkeypatch):
    a = api_admin

    def fail(db, *, values):
        raise RuntimeError("injected create failure")

    monkeypatch.setattr(store, "create_person", fail)
    with pytest.raises(boundary.HumanOperationError) as caught:
        commands.create_person(
            a.db, request=_request(a), email=f"cleanup-{uuid4().hex}@a.edu", first_name="A", last_name="B"
        )
    pending, seen, checked = [caught.value], set(), set()
    while pending:
        error = pending.pop()
        if id(error) in seen:
            continue
        seen.add(id(error))
        traceback = error.__traceback__
        while traceback is not None:
            frame = traceback.tb_frame
            if frame.f_code.co_filename in {boundary.__file__, commands.__file__}:
                checked.add(frame.f_code.co_name)
                values = dict(frame.f_locals)
                for name in (
                    "request",
                    "context",
                    "self",
                    "raw",
                    "digest",
                    "claims",
                    "candidate",
                    "resources",
                    "operation",
                ):
                    assert values.get(name) is None, "owned helper retained a request or command proof alias"
            traceback = traceback.tb_next
        pending.extend(value for value in (error.__cause__, error.__context__) if value is not None)
    assert {"_human_operation", "create_person"}.issubset(checked)
    a.db.commit()


@pytest.mark.parametrize("caught", [False, True])
def test_raw_matching_grant_without_fixed_command_never_reaches_audit_writer(api_admin, monkeypatch, caught):
    a = api_admin
    pid = _target(a, active=False, status="suspended")
    writer_calls = []
    real_writer = boundary.write_audit_event

    def observe(*args, **kwargs):
        writer_calls.append(kwargs["action"])
        return real_writer(*args, **kwargs)

    monkeypatch.setattr(boundary, "write_audit_event", observe)
    root = a.db.get_transaction()
    a.db.get(Role, a.admin_role_id).name = "Unrelated grant prior work"
    with pytest.raises(boundary.HumanOperationError):
        with boundary.grant_api_role(a.db, request=_request(a), person_id=pid, role_id=a.admin_role_id) as context:
            a.db.add(PartyRoleGrant(tenant_id=a.tid, party_id=pid, role_id=a.admin_role_id))
            a.db.flush()
            if caught:
                with pytest.raises(boundary.HumanOperationError):
                    context.audit()
            else:
                context.audit()
    assert writer_calls == [] and a.db.get_transaction() is root
    a.db.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert a.db.get(Role, a.admin_role_id).name == "Unrelated grant prior work"
    assert a.db.scalar(select(PartyRoleGrant.id).where(PartyRoleGrant.party_id == pid)) is None
    assert a.db.scalar(select(AuditEvent.id).where(AuditEvent.tenant_id == a.tid)) is None
    assert a.db.scalar(text("SELECT current_setting('app.current_tenant')")) == str(a.tid)


@pytest.mark.parametrize("mutation", ["profile", "subtype", "party_type"])
def test_genuine_grant_completion_requires_surviving_target_academy_inventory(api_admin, mutation):
    a = api_admin
    pid = _target(a, active=False, status="suspended")
    root = a.db.get_transaction()
    a.db.get(Role, a.admin_role_id).name = "Unrelated inventory prior work"
    with pytest.raises(boundary.HumanOperationError):
        with boundary.grant_api_role(a.db, request=_request(a), person_id=pid, role_id=a.admin_role_id) as context:
            context.grant_role()
            if mutation == "profile":
                a.db.delete(a.db.get(AcademyAccountProfile, pid))
            elif mutation == "subtype":
                a.db.delete(a.db.get(PartyPerson, pid))
            else:
                a.db.get(Party, pid).party_type = PartyType.organization
            a.db.flush()
            with pytest.raises(boundary.HumanOperationError):
                context.revalidate()
    assert a.db.get_transaction() is root
    a.db.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert a.db.get(Role, a.admin_role_id).name == "Unrelated inventory prior work"
    assert a.db.get(Party, pid).party_type == PartyType.person
    assert a.db.get(Party, pid).is_active is False
    assert a.db.get(PartyPerson, pid) is not None
    assert a.db.get(AcademyAccountProfile, pid).status == "suspended"
    assert a.db.scalar(select(PartyRoleGrant.id).where(PartyRoleGrant.party_id == pid)) is None
    assert a.db.scalar(select(AuditEvent.id).where(AuditEvent.tenant_id == a.tid)) is None
    assert a.db.scalar(text("SELECT current_setting('app.current_tenant')")) == str(a.tid)
