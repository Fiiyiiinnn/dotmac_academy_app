"""Self-serve onboarding portal — tokenized checklist → automatic enrolment.

Requires a migrated disposable Postgres (skipped otherwise by the fixtures).
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from app.models.cohort import Enrollment
from legacy_probe.conftest import client_for, csrf_for


def _csrf(web, token):
    assert web.get(f"/onboarding?token={token}").status_code == 200
    value = web.cookies.get("csrf_token")
    assert value
    return {"x-csrf-token": value}


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


def _applicant_in_onboarding(client, auth, admin_session, tenant, email):
    """Apply + advance to onboarding via the admin API; return (app_id, portal_token)."""
    from app.services import admissions as admissions_service

    cohort, track = _cohort(admin_session, tenant)
    a = client_for(TestClient(client.app), tenant.slug)
    admin_session.rollback()
    applicant = admissions_service.submit_application(
        admin_session,
        tenant_id=tenant.id,
        email=email,
        first_name="On",
        last_name="Boarder",
        cohort_id=cohort.id,
        track_id=track.id,
    )
    admin_session.commit()
    app_id = str(applicant.id)
    for nxt in ("screened", "accepted", "onboarding"):
        r = a.post(f"/admissions/{app_id}/transition", json={"to_status": nxt}, headers=auth)
        assert r.status_code == 200, r.text
    # The transition to onboarding minted the portal token; recover a fresh one
    # the same way the service does (the raw is only ever emailed).
    from uuid import UUID

    from app.models.admissions import Applicant

    admin_session.rollback()
    applicant = admin_session.get(Applicant, UUID(app_id))
    raw = admissions_service.mint_onboarding_token(admin_session, applicant=applicant)
    admin_session.commit()
    return app_id, raw


def test_portal_requires_valid_token(app_client, tenant_a):
    a = client_for(app_client, tenant_a.slug)
    r = a.get("/onboarding?token=not-a-token")
    assert r.status_code == 200
    assert "Link not valid" in r.text


def test_transition_to_onboarding_mints_portal_token(
    app_client,
    tenant_a,
    admin_session,
    api_actor,
):
    auth = api_actor(app_client, tenant_a)["headers"]
    a = client_for(TestClient(app_client.app), tenant_a.slug)
    from app.services import admissions as admissions_service

    cohort, track = _cohort(admin_session, tenant_a)
    applicant = admissions_service.submit_application(
        admin_session,
        tenant_id=tenant_a.id,
        email="mint@a.ex",
        first_name="Mi",
        last_name="Nt",
        cohort_id=cohort.id,
        track_id=track.id,
    )
    admin_session.commit()
    app_id = str(applicant.id)
    for nxt in ("screened", "accepted", "onboarding"):
        a.post(f"/admissions/{app_id}/transition", json={"to_status": nxt}, headers=auth)
    admin_session.rollback()
    got = admin_session.execute(
        text("SELECT onboarding_token_hash FROM applicants WHERE id=:a"), {"a": app_id}
    ).scalar()
    assert got  # minted on entering onboarding


def test_portal_renders_checklist(app_client, tenant_a, admin_session, api_actor):
    auth = api_actor(app_client, tenant_a)["headers"]
    app_id, token = _applicant_in_onboarding(app_client, auth, admin_session, tenant_a, "check@a.ex")
    a = client_for(TestClient(app_client.app), tenant_a.slug)
    r = a.get(f"/onboarding?token={token}")
    assert r.status_code == 200
    assert "Confirm your details" in r.text
    assert "Programme orientation" in r.text
    assert "check@a.ex" in r.text


def test_completing_checklist_auto_enrolls_and_invites(
    app_client,
    tenant_a,
    admin_session,
    api_actor,
):
    auth = api_actor(app_client, tenant_a)["headers"]
    app_id, token = _applicant_in_onboarding(app_client, auth, admin_session, tenant_a, "auto@a.ex")
    a = client_for(TestClient(app_client.app), tenant_a.slug)

    # No entrance exam configured for this cohort -> the seeded assessment task
    # still gates enrolment; complete it as the carry-forward would.
    admin_session.rollback()
    admin_session.execute(
        text("UPDATE onboarding_tasks SET status='done' " "WHERE applicant_id=:a AND key='entrance_assessment'"),
        {"a": app_id},
    )
    admin_session.commit()

    r = a.post("/onboarding/confirm", data={"token": token}, headers=_csrf(a, token), follow_redirects=False)
    assert r.status_code == 303
    # Not yet enrolled — orientation still pending.
    admin_session.rollback()
    status = admin_session.execute(text("SELECT status FROM applicants WHERE id=:a"), {"a": app_id}).scalar()
    assert status == "onboarding"

    r = a.post("/onboarding/orientation", data={"token": token}, headers=_csrf(a, token), follow_redirects=False)
    assert r.status_code == 303

    admin_session.rollback()
    row = admin_session.execute(text("SELECT status, person_id FROM applicants WHERE id=:a"), {"a": app_id}).one()
    assert row.status == "enrolled"
    assert row.person_id is not None
    # Enrolment artefacts: enrollment row, student role, and a password invite.
    enr = admin_session.execute(
        select(func.count()).select_from(Enrollment).where(Enrollment.person_id == row.person_id)
    ).scalar()
    assert enr == 1
    role = admin_session.execute(
        text(
            "SELECT count(*) FROM party_role_grants pr JOIN roles r ON r.id=pr.role_id "
            "WHERE pr.party_id=:p AND r.slug='student'"
        ),
        {"p": str(row.person_id)},
    ).scalar()
    assert role == 1
    invites = admin_session.execute(
        text("SELECT count(*) FROM account_tokens WHERE person_id=:p AND kind='invite'"),
        {"p": str(row.person_id)},
    ).scalar()
    assert invites == 1

    # Idempotent: re-posting completes nothing twice and never re-invites.
    a.post("/onboarding/orientation", data={"token": token}, headers=_csrf(a, token))
    admin_session.rollback()
    invites2 = admin_session.execute(
        text("SELECT count(*) FROM account_tokens WHERE person_id=:p AND kind='invite'"),
        {"p": str(row.person_id)},
    ).scalar()
    assert invites2 == 1

    # The portal now shows the enrolled state.
    r = a.get(f"/onboarding?token={token}")
    assert "enrolled" in r.text.lower()


def test_invite_token_activates_student_login(
    app_client,
    tenant_a,
    admin_session,
    api_actor,
):
    """The set-password link from auto-enrolment actually opens the account."""
    auth = api_actor(app_client, tenant_a)["headers"]
    app_id, token = _applicant_in_onboarding(app_client, auth, admin_session, tenant_a, "login@a.ex")
    a = client_for(TestClient(app_client.app), tenant_a.slug)
    admin_session.rollback()
    admin_session.execute(text("UPDATE onboarding_tasks SET status='done' WHERE applicant_id=:a"), {"a": app_id})
    admin_session.commit()
    # Any completed task POST triggers the auto-enrol check.
    a.post("/onboarding/confirm", data={"token": token}, headers=_csrf(a, token))

    admin_session.rollback()
    person_id = admin_session.execute(text("SELECT person_id FROM applicants WHERE id=:a"), {"a": app_id}).scalar()
    assert person_id is not None
    # Use the actual transactional welcome consequence, never a legacy re-mint.
    from urllib.parse import parse_qs, urlparse

    from sqlalchemy import select

    from app.models.email_outbox import EmailOutbox

    welcome = admin_session.scalars(
        select(EmailOutbox).where(
            EmailOutbox.tenant_id == tenant_a.id,
            EmailOutbox.recipient == "login@a.ex",
            EmailOutbox.kind == "enrollment_welcome",
        )
    ).one()
    setup_url = next(line for line in welcome.text_body.splitlines() if "/accept-invite?token=" in line)
    raw = parse_qs(urlparse(setup_url).query)["token"][0]

    fresh = client_for(TestClient(app_client.app), tenant_a.slug)
    # Follow the emailed link by path so the issued CSRF cookie lands on the
    # same host the POST uses (the link's absolute host is not the test base).
    link = urlparse(setup_url)
    # Pin the emailed host: it must be the tenant host this client addresses.
    assert (link.scheme, link.netloc) == (urlparse(str(fresh.base_url)).scheme, fresh.headers["Host"])
    assert fresh.get(f"{link.path}?{link.query}").status_code == 200
    r = fresh.post(
        "/accept-invite",
        data={"token": raw, "password": "a fine password"},
        headers={"x-csrf-token": csrf_for(fresh, {}, f"{link.path}?{link.query}")},
    )
    assert r.status_code == 200, r.text
    login = fresh.post("/auth/login", json={"email": "login@a.ex", "password": "a fine password"})
    assert login.status_code == 200, login.text
