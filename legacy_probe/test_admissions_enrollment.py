"""Admissions P2 — applicant -> enrolled learner conversion.

Requires a migrated disposable Postgres (skipped otherwise by the fixtures).
"""

from __future__ import annotations

from dotmac_kernel.models import Party
from sqlalchemy import func, select

from app.models.cohort import Enrollment
from legacy_probe.conftest import client_for, make_person_party


def _cohort(admin_session, tenant, name="Fiber intake"):
    from app.models.cohort import Cohort
    from app.models.track import CohortTrack, Track

    admin_session.rollback()
    c = Cohort(tenant_id=tenant.id, name=name, discipline="fiber", status="active")
    admin_session.add(c)
    admin_session.flush()
    track = Track(
        tenant_id=tenant.id,
        slug=f"fiber-{c.id}",
        name="Fiber",
        status="active",
    )
    admin_session.add(track)
    admin_session.flush()
    admin_session.add(
        CohortTrack(
            tenant_id=tenant.id,
            cohort_id=c.id,
            track_id=track.id,
            status="active",
        )
    )
    admin_session.commit()
    admin_session.refresh(c)
    admin_session.refresh(track)
    return c, track

def _application(admin_session, tenant, cohort, track, email):
    from app.services import admissions

    admin_session.rollback()
    applicant = admissions.submit_application(
        admin_session,
        tenant_id=tenant.id,
        email=email,
        first_name="Test",
        last_name="Applicant",
        cohort_id=cohort.id,
        track_id=track.id,
    )
    admin_session.commit()
    return str(applicant.id)

def _clear_onboarding(client, auth, app_id):
    """Mark every onboarding task done via the API.

    The GET sets a csrf_token cookie; once cookies exist the CSRF middleware
    enforces the double-submit on POSTs, so we echo the token. Clearing cookies
    afterwards keeps the rest of the (Bearer-auth) API flow cookie-free.
    """
    tasks = client.get(f"/admissions/{app_id}/onboarding", headers=auth).json()
    csrf = client.cookies.get("csrf_token", "")
    for t in tasks:
        client.post(
            f"/admissions/onboarding-tasks/{t['id']}",
            json={"status": "done"},
            headers={**auth, "x-csrf-token": csrf},
        )
    client.cookies.clear()

def _to_onboarding(client, auth, app_id):
    for nxt in ("screened", "accepted", "onboarding"):
        client.post(f"/admissions/{app_id}/transition", json={"to_status": nxt}, headers=auth)
    _clear_onboarding(client, auth, app_id)

def test_enroll_creates_person_and_enrollment(app_client, tenant_a, admin_session, api_actor):
    a = client_for(app_client, tenant_a.slug)
    auth = api_actor(app_client, tenant_a)["headers"]
    cohort, track = _cohort(admin_session, tenant_a)
    app_id = _application(admin_session, tenant_a, cohort, track, "learn@a.ex")
    _to_onboarding(a, auth, app_id)

    r = a.post(f"/admissions/{app_id}/enroll", json={"cohort_id": str(cohort.id)}, headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "enrolled"
    assert body["person_id"] is not None

    # A Person + Enrollment now exist for this email/cohort.
    admin_session.rollback()
    pid = admin_session.execute(select(Party.id).where(Party.email == "learn@a.ex")).scalar()
    assert pid is not None
    n = admin_session.execute(
        select(func.count()).select_from(Enrollment).where(Enrollment.person_id == pid, Enrollment.cohort_id == cohort.id)
    ).scalar()
    assert n == 1

def test_enroll_is_idempotent(app_client, tenant_a, admin_session, api_actor):
    a = client_for(app_client, tenant_a.slug)
    auth = api_actor(app_client, tenant_a)["headers"]
    cohort, track = _cohort(admin_session, tenant_a)
    app_id = _application(admin_session, tenant_a, cohort, track, "idem@a.ex")
    _to_onboarding(a, auth, app_id)

    first = a.post(f"/admissions/{app_id}/enroll", json={"cohort_id": str(cohort.id)}, headers=auth)
    # Re-enrol (applicant already 'enrolled') is rejected by the status guard...
    again = a.post(f"/admissions/{app_id}/enroll", json={"cohort_id": str(cohort.id)}, headers=auth)
    assert first.status_code == 200
    assert again.status_code == 400  # not in 'onboarding' anymore

    admin_session.rollback()
    n = admin_session.execute(
        select(func.count()).select_from(Enrollment).where(Enrollment.cohort_id == cohort.id)
    ).scalar()
    assert n == 1  # no duplicate enrolment

def test_enroll_requires_onboarding_status(app_client, tenant_a, admin_session, api_actor):
    a = client_for(app_client, tenant_a.slug)
    auth = api_actor(app_client, tenant_a)["headers"]
    cohort, track = _cohort(admin_session, tenant_a)
    app_id = _application(admin_session, tenant_a, cohort, track, "early@a.ex")
    # still 'applied' — enrol must fail
    r = a.post(f"/admissions/{app_id}/enroll", json={"cohort_id": str(cohort.id)}, headers=auth)
    assert r.status_code == 400

def test_enroll_blocked_by_incomplete_onboarding(app_client, tenant_a, admin_session, api_actor):
    a = client_for(app_client, tenant_a.slug)
    auth = api_actor(app_client, tenant_a)["headers"]
    cohort, track = _cohort(admin_session, tenant_a)
    app_id = _application(admin_session, tenant_a, cohort, track, "wip@a.ex")
    # Reach onboarding but leave the checklist unfinished.
    for nxt in ("screened", "accepted", "onboarding"):
        a.post(f"/admissions/{app_id}/transition", json={"to_status": nxt}, headers=auth)
    tasks = a.get(f"/admissions/{app_id}/onboarding", headers=auth).json()
    assert len(tasks) >= 1 and all(t["status"] == "pending" for t in tasks)
    a.cookies.clear()  # keep the enrol POST cookie-free (Bearer API, no CSRF)
    r = a.post(f"/admissions/{app_id}/enroll", json={"cohort_id": str(cohort.id)}, headers=auth)
    assert r.status_code == 400  # outstanding onboarding tasks

def test_enroll_reuses_existing_person(app_client, tenant_a, admin_session, api_actor):
    """An email that is already a Person (e.g. an employee) is reused, not duplicated."""

    a = client_for(app_client, tenant_a.slug)
    auth = api_actor(app_client, tenant_a)["headers"]
    cohort, track = _cohort(admin_session, tenant_a)

    admin_session.rollback()
    existing = make_person_party(admin_session, tenant_id=tenant_a.id, email="staff@a.ex", first_name="Staff", last_name="Member")
    admin_session.add(existing)
    admin_session.commit()
    admin_session.refresh(existing)

    app_id = _application(admin_session, tenant_a, cohort, track, "staff@a.ex")
    _to_onboarding(a, auth, app_id)
    r = a.post(f"/admissions/{app_id}/enroll", json={"cohort_id": str(cohort.id)}, headers=auth)
    assert r.status_code == 200
    assert r.json()["person_id"] == str(existing.id)

    admin_session.rollback()
    n = admin_session.execute(
        select(func.count()).select_from(Party).where(Party.email == "staff@a.ex")
    ).scalar()
    assert n == 1  # not duplicated
