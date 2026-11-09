"""Admissions: cross-tenant isolation + pipeline canaries.

Mirrors the other ``test_*_isolation.py`` files. Requires a migrated disposable
Postgres (TEST_DATABASE_URL); skipped otherwise by the shared fixtures.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from legacy_probe.conftest import client_for


def _application(
    admin_session,
    tenant,
    email,
    *,
    last_name="Applicant",
    cohort=None,
    track=None,
):
    from app.services import admissions

    applicant = admissions.submit_application(
        admin_session,
        tenant_id=tenant.id,
        email=email,
        first_name="Test",
        last_name=last_name,
        cohort_id=cohort.id if cohort else None,
        track_id=track.id if track else None,
    )
    admin_session.commit()
    return applicant


def _intake(admin_session, tenant):
    from app.models.cohort import Cohort
    from app.models.track import CohortTrack, Track

    cohort = Cohort(
        tenant_id=tenant.id,
        name="Admissions Intake",
        discipline="fiber",
        status="active",
    )
    track = Track(
        tenant_id=tenant.id,
        slug="admissions-fiber",
        name="Fiber Installer",
        status="active",
    )
    admin_session.add_all([cohort, track])
    admin_session.flush()
    admin_session.add(
        CohortTrack(
            tenant_id=tenant.id,
            cohort_id=cohort.id,
            track_id=track.id,
            status="active",
        )
    )
    admin_session.commit()
    return cohort, track


def test_public_admissions_api_has_no_intake_writer(app_client, tenant_a):
    a = client_for(app_client, tenant_a.slug)
    r = a.post(
        "/admissions/apply",
        json={
            "email": "ANN@a.example",
            "first_name": "Ann",
            "last_name": "A",
            "phone": "0800",
            "program": "Fiber Academy",
        },
    )
    assert r.status_code == 405


def test_application_service_is_idempotent_on_email(admin_session, tenant_a):
    first = _application(
        admin_session,
        tenant_a,
        "dup@a.example",
        last_name="One",
    )
    again = _application(
        admin_session,
        tenant_a,
        "dup@a.example",
        last_name="Two",
    )
    assert first.id == again.id


def test_applicant_isolated_between_tenants(
    app_client,
    tenant_a,
    tenant_b,
    admin_session,
    api_actor,
):
    app_id = str(_application(admin_session, tenant_a, "sec@a.example").id)

    b = client_for(TestClient(app_client.app), tenant_b.slug)
    auth_b = api_actor(app_client, tenant_b)["headers"]
    assert b.get(f"/admissions/{app_id}", headers=auth_b).status_code == 404
    listed = b.get("/admissions", headers=auth_b).json()
    assert app_id not in [x["id"] for x in listed]


def test_pipeline_transitions_and_guards(app_client, tenant_a, admin_session, api_actor):
    a = client_for(app_client, tenant_a.slug)
    auth = api_actor(app_client, tenant_a)["headers"]
    cohort, track = _intake(admin_session, tenant_a)
    app_id = str(
        _application(
            admin_session,
            tenant_a,
            "flow@a.example",
            cohort=cohort,
            track=track,
        ).id
    )

    # Illegal jump applied -> enrolled is rejected.
    bad = a.post(f"/admissions/{app_id}/transition", json={"to_status": "enrolled"}, headers=auth)
    assert bad.status_code == 400

    # Walk the happy path applied -> screened -> accepted -> onboarding.
    for nxt in ("screened", "accepted", "onboarding"):
        r = a.post(f"/admissions/{app_id}/transition", json={"to_status": nxt}, headers=auth)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == nxt

    # Unknown status -> 400.
    assert a.post(f"/admissions/{app_id}/transition", json={"to_status": "banana"}, headers=auth).status_code == 400


def test_admissions_api_requires_admin(app_client, tenant_a):
    a = client_for(app_client, tenant_a.slug)
    assert a.get("/admissions").status_code == 401


def test_api_cannot_accept_applicant_without_canonical_intake(
    app_client,
    tenant_a,
    admin_session,
    api_actor,
):
    a = client_for(app_client, tenant_a.slug)
    auth = api_actor(app_client, tenant_a)["headers"]
    applicant = _application(admin_session, tenant_a, "unassigned@a.example")

    screened = a.post(
        f"/admissions/{applicant.id}/transition",
        json={"to_status": "screened"},
        headers=auth,
    )
    assert screened.status_code == 200
    accepted = a.post(
        f"/admissions/{applicant.id}/transition",
        json={"to_status": "accepted"},
        headers=auth,
    )
    assert accepted.status_code == 400
    assert "canonical training track" in accepted.json()["message"]
