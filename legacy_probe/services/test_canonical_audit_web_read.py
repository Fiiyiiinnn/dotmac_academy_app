"""Hosted read canaries: real canonical rows and actual cookie authentication.

Direct historical rows below are read fixtures, never sanctioned writer proof.
Test-local roles do not prove configured production bootstrap.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from dotmac_kernel.audit import AuditEvent
from dotmac_kernel.models import Party, PartyPerson, PartyRoleGrant, PartyType, Role, UserCredential
from sqlalchemy import JSON, null, select, update

from app.models.identity_extensions import AcademyAccountProfile, AcademyCredentialPolicy
from app.services.api_identity_reads import count_audit_events, existing_people, list_audit_events
from app.services.audit import list_events
from app.services.security import hash_password
from legacy_probe.conftest import web_login


def seed_person(db, *, tenant_id, email, first_name, last_name, profile=True, active=True, status="active"):
    now = datetime.now(UTC)
    party = Party(
        tenant_id=tenant_id, email=email, party_type=PartyType.person,
        display_name=f"{first_name} {last_name}", is_active=active,
    )
    db.add(party)
    db.flush()
    db.add(PartyPerson(party_id=party.id, first_name=first_name, last_name=last_name))
    db.flush()
    if profile:
        db.add(AcademyAccountProfile(
            tenant_id=tenant_id, party_id=party.id, status=status, prefs={}, created_at=now, updated_at=now,
        ))
        db.flush()
    return party


def seed_login(db, tenant, email, role_slug):
    party = seed_person(db, tenant_id=tenant.id, email=email, first_name="Test", last_name="User")
    now = datetime.now(UTC)
    credential = UserCredential(tenant_id=tenant.id, party_id=party.id, password_hash=hash_password("password1"))
    db.add(credential)
    db.flush()
    db.add(AcademyCredentialPolicy(
        tenant_id=tenant.id, credential_id=credential.id, email=email, failed_login_attempts=0,
        lifecycle_version=0, reset_required=False, locked_until=None, created_at=now, updated_at=now,
    ))
    role = db.scalars(select(Role).where(Role.tenant_id == tenant.id, Role.slug == role_slug)).first()
    if role is None:
        role = Role(tenant_id=tenant.id, slug=role_slug, name=role_slug)
        db.add(role)
        db.flush()
    db.add(PartyRoleGrant(tenant_id=tenant.id, party_id=party.id, role_id=role.id))
    db.commit()
    return party


def seed_historical_event(db, *, tenant_id, actor_person_id=None, action, entity_type, entity_id=None, **facts):
    facts.setdefault("created_at", datetime.now(UTC))
    event = AuditEvent(
        tenant_id=tenant_id, actor_party_id=actor_person_id, action=action,
        entity_type=entity_type, entity_id=entity_id, **facts,
    )
    db.add(event)
    db.flush()
    return event


def _login(client, email="admin@a.edu"):
    headers = {"Host": "alpha.localhost"}
    web_login(client, headers, email, "password1")
    assert client.cookies.get("session")
    return headers


@pytest.mark.parametrize("actor_type", ["user", "service", "api_key", "system"])
@pytest.mark.parametrize("party_state", ["deleted", "missing_profile", "absent"])
def test_stored_forensic_actor_survives_inventory_loss(app_client, admin_session, tenant_a, actor_type, party_state):
    seed_login(admin_session, tenant_a, "admin@a.edu", "admin")
    party_id = uuid4()
    if party_state != "absent":
        party = seed_person(admin_session, tenant_id=tenant_a.id, email="old@a.edu",
                            first_name="Old", last_name="Actor", profile=party_state != "missing_profile")
        party_id = party.id
        if party_state == "deleted":
            admin_session.delete(party)
            admin_session.flush()
    seed_historical_event(admin_session, tenant_id=tenant_a.id, actor_person_id=party_id,
                          action="forensic.survival", entity_type="test", actor_type=actor_type,
                          actor_id="immutable-principal", actor_label="Stored snapshot")
    admin_session.commit()
    response = app_client.get("/admin/audit?action=forensic.survival", headers=_login(app_client))
    assert response.status_code == 200
    assert "Stored snapshot" in response.text
    assert actor_type in response.text and "immutable-principal" in response.text
    assert "Automated action" not in response.text


@pytest.mark.parametrize("facts,expected", [
    ({}, "Automated action"),
    ({"actor_label": "Historical snapshot"}, "Historical snapshot"),
    ({"actor_id": "untyped-id"}, "untyped-id"),
    ({"actor_type": "user"}, "user"),
    ({"actor_party_id": "missing"}, "Unknown actor"),
])
def test_partial_historical_actor_never_invents_identity(app_client, admin_session, tenant_a, facts, expected):
    seed_login(admin_session, tenant_a, "admin@a.edu", "admin")
    facts = dict(facts)
    party_id = uuid4() if facts.pop("actor_party_id", None) else None
    seed_historical_event(admin_session, tenant_id=tenant_a.id, actor_person_id=party_id,
                          action="historical.partial", entity_type="test", **facts)
    admin_session.commit()
    response = app_client.get("/admin/audit?action=historical.partial", headers=_login(app_client))
    assert response.status_code == 200 and expected in response.text
    if facts or party_id:
        assert "Automated action" not in response.text
    if not facts.get("actor_type"):
        assert "cell-secondary font-mono\">user" not in response.text
        assert "cell-secondary font-mono\">system" not in response.text


def test_nullable_time_and_raw_list_and_json_null_details(app_client, admin_session, tenant_a):
    seed_login(admin_session, tenant_a, "admin@a.edu", "admin")
    event = seed_historical_event(admin_session, tenant_id=tenant_a.id, action="historical.raw",
                                  entity_type="test", details=["original", {"nested": [1, None]}])
    empty = seed_historical_event(admin_session, tenant_id=tenant_a.id, action="historical.null",
                                  entity_type="test")
    # Explicit historical unknown time and JSON null; never a current writer call.
    admin_session.execute(update(AuditEvent).where(AuditEvent.id == event.id).values(created_at=null()))
    admin_session.execute(update(AuditEvent).where(AuditEvent.id == empty.id).values(details=JSON.NULL))
    admin_session.commit()
    views = list_events(admin_session, tenant_id=tenant_a.id, action="historical.raw")
    assert views[0].created_at is None
    assert views[0].details == ["original", {"nested": [1, None]}]
    views[0].details[1]["nested"].append(99)
    assert list_events(admin_session, tenant_id=tenant_a.id, action="historical.raw")[0].details[1]["nested"] == [1, None]
    api = {view.action: view for view in list_audit_events(admin_session, tenant_id=tenant_a.id)}
    assert api["historical.raw"].created_at is None
    api["historical.raw"].details[1]["nested"].append(88)
    fresh = {view.action: view for view in list_audit_events(admin_session, tenant_id=tenant_a.id)}
    assert fresh["historical.raw"].details == ["original", {"nested": [1, None]}]
    assert api["historical.null"].details is None
    headers = _login(app_client)
    response = app_client.get("/admin/audit?action=historical.raw", headers=headers)
    assert response.status_code == 200 and "original" in response.text and "—" in response.text
    response = app_client.get("/admin/audit?action=historical.null", headers=headers)
    assert response.status_code == 200 and "—" in response.text


def test_email_inventory_scope_count_pages_and_inactive_profile(app_client, admin_session, tenant_a, tenant_b):
    seed_login(admin_session, tenant_a, "admin@a.edu", "admin")
    actor = seed_person(admin_session, tenant_id=tenant_a.id, email="inactive@a.edu", first_name="Inactive",
                        last_name="Actor", active=False, status="suspended")
    foreign = seed_person(admin_session, tenant_id=tenant_b.id, email="foreign@b.edu", first_name="Foreign",
                          last_name="Actor")
    no_profile = seed_person(admin_session, tenant_id=tenant_a.id, email="missing@a.edu", first_name="Missing",
                             last_name="Profile", profile=False)
    now = datetime.now(UTC)
    for i in range(3):
        seed_historical_event(admin_session, tenant_id=tenant_a.id, actor_person_id=actor.id,
                              action="page.match", entity_type="test", actor_type="user", actor_id="inactive-user",
                              created_at=now + timedelta(seconds=i))
    seed_historical_event(admin_session, tenant_id=tenant_a.id, actor_person_id=foreign.id,
                          action="foreign.enrichment", entity_type="test", actor_type="user", actor_id="foreign-id")
    seed_historical_event(admin_session, tenant_id=tenant_a.id, actor_person_id=no_profile.id,
                          action="missing.profile", entity_type="test", actor_type="user", actor_id="missing-id")
    seed_historical_event(admin_session, tenant_id=tenant_a.id, actor_person_id=foreign.id,
                          action="page.match", entity_type="test")
    seed_historical_event(admin_session, tenant_id=tenant_b.id, action="page.match", entity_type="test")
    seed_historical_event(admin_session, tenant_id=tenant_b.id, action="foreign.tenant", entity_type="test")
    admin_session.commit()
    assert existing_people(admin_session, tenant_id=tenant_a.id, email="inactive@a.edu")[0].id == actor.id
    assert not existing_people(admin_session, tenant_id=tenant_a.id, person_ids={foreign.id, no_profile.id})
    assert count_audit_events(admin_session, tenant_id=tenant_a.id, action="page.match", actor_party_id=actor.id) == 3
    headers = _login(app_client)
    first = app_client.get("/admin/audit?action=page.match&actor_email=inactive@a.edu&limit=2", headers=headers)
    second = app_client.get("/admin/audit?action=page.match&actor_email=inactive@a.edu&limit=2&offset=2", headers=headers)
    assert first.status_code == second.status_code == 200
    assert len(first.context["events"]) == 2 and len(second.context["events"]) == 1
    assert first.context["pagination"]["total"] == second.context["pagination"]["total"] == 3
    assert {e.id for e in first.context["events"]}.isdisjoint({e.id for e in second.context["events"]})
    for email in ["unknown@a.edu", "foreign@b.edu", "missing@a.edu", "Inactive@a.edu"]:
        response = app_client.get("/admin/audit", headers=headers, params={"actor_email": email})
        assert response.status_code == 200 and response.context["events"] == []
        assert response.context["pagination"]["total"] == 0
    response = app_client.get("/admin/audit", headers=headers)
    assert foreign.id not in response.context["actor_map"] and no_profile.id not in response.context["actor_map"]
    assert "foreign@b.edu" not in response.text and "missing@a.edu" not in response.text
    assert "foreign.tenant" not in response.text


@pytest.mark.parametrize("actor_type", ["user", "service", "api_key", "system"])
def test_unlabelled_canonical_actor_retains_actual_pair(app_client, admin_session, tenant_a, actor_type):
    seed_login(admin_session, tenant_a, "admin@a.edu", "admin")
    seed_historical_event(admin_session, tenant_id=tenant_a.id, action="canonical.unlabelled", entity_type="test",
                          actor_type=actor_type, actor_id="stable-recorded-id")
    admin_session.commit()
    response = app_client.get("/admin/audit?action=canonical.unlabelled", headers=_login(app_client))
    assert response.status_code == 200
    assert actor_type in response.text and "stable-recorded-id" in response.text
    assert "Automated action" not in response.text
    if actor_type != "system":
        assert '<p class="cell-primary">System</p>' not in response.text
