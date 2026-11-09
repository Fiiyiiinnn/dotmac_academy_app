"""Tests for the admin applications page."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from dotmac_kernel.models import PartyRoleGrant
from sqlalchemy import select

from app.models.assessment import Question, QuestionBank
from app.models.cohort import Cohort
from app.models.course import Course
from app.models.email_outbox import EmailOutbox
from app.models.rbac import AuditEvent
from app.models.track import CohortTrack, Track
from app.services import admissions
from app.services.bootstrap import ensure_roles
from app.services.entrance_exam import set_academy_defaults
from app.services.security import hash_password
from legacy_probe.conftest import csrf_for, make_person_credential, make_person_party, web_login


def _seed_user(admin_session, tenant, email, role_slug):
    roles = ensure_roles(admin_session, tenant.id)
    person = make_person_party(admin_session, tenant_id=tenant.id, email=email, first_name="Seed", last_name="User")
    admin_session.add(person)
    admin_session.flush()
    admin_session.add(
        make_person_credential(admin_session, tenant_id=tenant.id, party_id=person.id, email=email, password_hash=hash_password("password1"))
    )
    admin_session.add(PartyRoleGrant(tenant_id=tenant.id, party_id=person.id, role_id=roles[role_slug].id))
    admin_session.commit()
    return person

def _login(app_client, email):
    headers = {"Host": "alpha.localhost"}
    web_login(app_client, headers, email, "password1")
    return headers

def _intake(admin_session, tenant):
    cohort = Cohort(
        tenant_id=tenant.id,
        name="Abuja Intake",
        discipline="fiber",
        status="active",
    )
    track = Track(
        tenant_id=tenant.id,
        slug="fiber-installer",
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

def test_admin_can_view_applications_page(app_client, admin_session, tenant_a):
    _seed_user(admin_session, tenant_a, "apps-admin@a.edu", "admin")
    admissions.submit_application(
        admin_session,
        tenant_id=tenant_a.id,
        email="candidate@a.edu",
        first_name="Candidate",
        last_name="One",
        phone="08000000000",
        program="Fiber Academy",
    )
    admin_session.commit()

    response = app_client.get("/admin/applications", headers=_login(app_client, "apps-admin@a.edu"))

    assert response.status_code == 200
    assert "Applications" in response.text
    assert "candidate@a.edu" in response.text
    assert "Applicant pipeline" in response.text

def test_admin_can_search_applications_by_applicant_name(app_client, admin_session, tenant_a):
    _seed_user(admin_session, tenant_a, "apps-search-admin@a.edu", "admin")
    admissions.submit_application(
        admin_session,
        tenant_id=tenant_a.id,
        email="amina@a.edu",
        first_name="Amina",
        last_name="Yusuf",
    )
    admissions.submit_application(
        admin_session,
        tenant_id=tenant_a.id,
        email="bala@a.edu",
        first_name="Bala",
        last_name="Okoro",
    )
    admin_session.commit()

    response = app_client.get(
        "/admin/applications?q=amina",
        headers=_login(app_client, "apps-search-admin@a.edu"),
    )

    assert response.status_code == 200
    assert "amina@a.edu" in response.text
    assert "bala@a.edu" not in response.text
    assert 'value="amina"' in response.text

def test_admin_can_filter_applications_by_applied_date_range(app_client, admin_session, tenant_a):
    _seed_user(admin_session, tenant_a, "apps-date-admin@a.edu", "admin")
    admissions.submit_application(
        admin_session,
        tenant_id=tenant_a.id,
        email="early@a.edu",
        first_name="Early",
        last_name="Applicant",
        applied_on=date(2026, 1, 10),
    )
    admissions.submit_application(
        admin_session,
        tenant_id=tenant_a.id,
        email="inside@a.edu",
        first_name="Inside",
        last_name="Applicant",
        applied_on=date(2026, 2, 15),
    )
    admissions.submit_application(
        admin_session,
        tenant_id=tenant_a.id,
        email="late@a.edu",
        first_name="Late",
        last_name="Applicant",
        applied_on=date(2026, 3, 10),
    )
    admin_session.commit()

    response = app_client.get(
        "/admin/applications?applied_from=2026-02-01&applied_to=2026-02-28",
        headers=_login(app_client, "apps-date-admin@a.edu"),
    )

    assert response.status_code == 200
    assert "inside@a.edu" in response.text
    assert "early@a.edu" not in response.text
    assert "late@a.edu" not in response.text
    assert 'value="2026-02-01"' in response.text
    assert 'value="2026-02-28"' in response.text

def test_reversed_application_date_range_shows_validation_message(app_client, admin_session, tenant_a):
    _seed_user(admin_session, tenant_a, "apps-reversed-date-admin@a.edu", "admin")
    admissions.submit_application(
        admin_session,
        tenant_id=tenant_a.id,
        email="candidate-reversed@a.edu",
        first_name="Range",
        last_name="Candidate",
        applied_on=date(2026, 7, 28),
    )
    admin_session.commit()

    response = app_client.get(
        "/admin/applications?applied_from=2026-07-28&applied_to=2026-07-27",
        headers=_login(app_client, "apps-reversed-date-admin@a.edu"),
    )

    assert response.status_code == 200
    assert "Start date must be before or the same as end date." in response.text
    assert "candidate-reversed@a.edu" not in response.text
    assert 'value="2026-07-28"' in response.text
    assert 'value="2026-07-27"' in response.text

def test_student_cannot_view_applications_page(app_client, admin_session, tenant_a):
    _seed_user(admin_session, tenant_a, "apps-student@a.edu", "student")

    response = app_client.get("/admin/applications", headers=_login(app_client, "apps-student@a.edu"))

    assert response.status_code == 403

def test_admin_assigns_canonical_intake_then_accepts_with_audited_history(
    app_client,
    admin_session,
    tenant_a,
):
    actor = _seed_user(admin_session, tenant_a, "review-admin@a.edu", "admin")
    cohort, track = _intake(admin_session, tenant_a)
    applicant = admissions.submit_application(
        admin_session,
        tenant_id=tenant_a.id,
        email="legacy-candidate@a.edu",
        first_name="Legacy",
        last_name="Candidate",
    )
    admin_session.commit()
    applicant_id = applicant.id

    headers = _login(app_client, "review-admin@a.edu")
    detail = app_client.get(
        f"/admin/applications/{applicant_id}",
        headers=headers,
    )
    assert detail.status_code == 200
    assert "Training placement" in detail.text
    assert "Accept and invite to onboarding" not in detail.text
    assert f'hx-post="/admin/applications/{applicant_id}/intake"' in detail.text
    assert f'hx-post="/admin/applications/{applicant_id}/action"' in detail.text
    assert "/static/csrf.js" in detail.text  # the external CSRF shim is loaded

    csrf = csrf_for(app_client, headers, f"/admin/applications/{applicant_id}")
    assigned = app_client.post(
        f"/admin/applications/{applicant_id}/intake",
        headers={**headers, "x-csrf-token": csrf},
        data={
            "intake_choice": f"{cohort.id}:{track.id}",
            "reason": "Mapped legacy application",
        },
        follow_redirects=False,
    )
    assert assigned.status_code == 303

    accepted = app_client.post(
        f"/admin/applications/{applicant_id}/action",
        headers={**headers, "x-csrf-token": csrf},
        data={"action": "accept", "reason": "Assessment reviewed"},
        follow_redirects=False,
    )
    assert accepted.status_code == 303
    assert "token=" not in accepted.text

    admin_session.rollback()
    admin_session.refresh(applicant)
    assert applicant.cohort_id == cohort.id
    assert applicant.track_id == track.id
    assert applicant.program == "Fiber Installer"
    assert applicant.status == "onboarding"

    events = list(
        admin_session.scalars(
            select(AuditEvent)
            .where(AuditEvent.tenant_id == tenant_a.id)
            .where(AuditEvent.entity_id == str(applicant_id))
            .order_by(AuditEvent.created_at)
        ).all()
    )
    assert events[0].action == "applicant.intake_assigned"
    assert events[0].actor_party_id == actor.id
    assert events[0].details["reason"] == "Mapped legacy application"
    transitions = [event for event in events if event.action == "applicant.transition"]
    assert [event.details["to_status"] for event in transitions] == [
        "screened",
        "accepted",
        "onboarding",
    ]
    assert all(event.details["source"] == "admin_web" for event in transitions)

    invitation = admin_session.scalars(
        select(EmailOutbox)
        .where(EmailOutbox.tenant_id == tenant_a.id)
        .where(EmailOutbox.kind == "onboarding_invite")
    ).one()
    assert invitation.recipient == applicant.email

def _entrance_config(admin_session, tenant):
    course = Course(
        tenant_id=tenant.id,
        slug="entrance-export-test",
        title="Entrance Export Test",
        discipline="fiber",
        source_ref="test",
        version=1,
        status="published",
        listed=False,
    )
    admin_session.add(course)
    admin_session.flush()
    bank = QuestionBank(tenant_id=tenant.id, course_id=course.id, chapter_number=1, kind="chapter", version=1)
    admin_session.add(bank)
    admin_session.flush()
    admin_session.add(
        Question(
            tenant_id=tenant.id,
            bank_id=bank.id,
            ext_id="q1",
            stem="Pick A",
            type="single",
            options=["A", "B"],
            correct=["A"],
            rubric_category="recall",
            explanation="Because A",
            weight=1,
        )
    )
    set_academy_defaults(
        admin_session, tenant_id=tenant.id, bank_id=bank.id, time_limit_minutes=30
    )
    admin_session.flush()
    return bank

def test_admin_resends_invitation_extends_deadline_and_audits(app_client, admin_session, tenant_a):
    actor = _seed_user(admin_session, tenant_a, "resend-admin@a.edu", "admin")
    _entrance_config(admin_session, tenant_a)
    applicant = admissions.submit_application(
        admin_session,
        tenant_id=tenant_a.id,
        email="resend-candidate@a.edu",
        first_name="Resend",
        last_name="Candidate",
    )
    applicant.assessment_token_hash = "old-token-hash"
    applicant.assessment_deadline = datetime.now(UTC) - timedelta(days=1)
    admin_session.commit()

    headers = _login(app_client, "resend-admin@a.edu")
    app_client.get(f"/admin/applications/{applicant.id}", headers=headers)
    csrf = app_client.cookies.get("csrf_token", "")
    response = app_client.post(
        f"/admin/applications/{applicant.id}/action",
        headers={**headers, "x-csrf-token": csrf},
        data={"action": "resend_invitation", "extend_access": "true", "reason": "Expired link"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    admin_session.rollback()
    admin_session.refresh(applicant)
    assert applicant.assessment_token_hash != "old-token-hash"
    assert applicant.assessment_deadline > datetime.now(UTC)
    mail = admin_session.scalars(
        select(EmailOutbox).where(EmailOutbox.recipient == "resend-candidate@a.edu")
    ).one()
    assert mail.kind == "entrance_invite"
    assert "Resend" in mail.text_body
    assert "Entrance Export Test" in mail.text_body
    assert "Duration: 30 minutes" in mail.text_body
    event = admin_session.scalars(
        select(AuditEvent).where(AuditEvent.entity_id == str(applicant.id)).where(AuditEvent.action == "applicant.invitation_resent")
    ).one()
    assert event.actor_party_id == actor.id
    assert event.details["extended_access"] is True

def test_resend_invitation_without_extension_preserves_active_deadline(app_client, admin_session, tenant_a):
    _seed_user(admin_session, tenant_a, "resend-noextend-admin@a.edu", "admin")
    _entrance_config(admin_session, tenant_a)
    applicant = admissions.submit_application(
        admin_session,
        tenant_id=tenant_a.id,
        email="resend-noextend@a.edu",
        first_name="Noextend",
        last_name="Candidate",
    )
    applicant.assessment_token_hash = "old-active-token-hash"
    original_deadline = datetime.now(UTC) + timedelta(days=3)
    applicant.assessment_deadline = original_deadline
    admin_session.commit()

    headers = _login(app_client, "resend-noextend-admin@a.edu")
    app_client.get(f"/admin/applications/{applicant.id}", headers=headers)
    csrf = app_client.cookies.get("csrf_token", "")
    response = app_client.post(
        f"/admin/applications/{applicant.id}/action",
        headers={**headers, "x-csrf-token": csrf},
        data={"action": "resend_invitation", "reason": "Lost email"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    admin_session.rollback()
    admin_session.refresh(applicant)
    assert applicant.assessment_token_hash != "old-active-token-hash"
    assert applicant.assessment_deadline == original_deadline
    event = admin_session.scalars(
        select(AuditEvent)
        .where(AuditEvent.entity_id == str(applicant.id))
        .where(AuditEvent.action == "applicant.invitation_resent")
    ).one()
    assert event.details["extended_access"] is False

def test_admin_extends_access_without_resetting_completed_work(app_client, admin_session, tenant_a):
    _seed_user(admin_session, tenant_a, "extend-admin@a.edu", "admin")
    _entrance_config(admin_session, tenant_a)
    applicant = admissions.submit_application(
        admin_session,
        tenant_id=tenant_a.id,
        email="extend-candidate@a.edu",
        first_name="Extend",
        last_name="Candidate",
    )
    applicant.assessment_token_hash = "existing-token-hash"
    applicant.assessment_answers = {"q1": ["A"]}
    applicant.assessment_deadline = datetime.now(UTC) - timedelta(days=1)
    admin_session.commit()

    headers = _login(app_client, "extend-admin@a.edu")
    app_client.get(f"/admin/applications/{applicant.id}", headers=headers)
    csrf = app_client.cookies.get("csrf_token", "")
    response = app_client.post(
        f"/admin/applications/{applicant.id}/action",
        headers={**headers, "x-csrf-token": csrf},
        data={"action": "extend_access", "reason": "More time"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    admin_session.rollback()
    admin_session.refresh(applicant)
    assert applicant.assessment_token_hash == "existing-token-hash"
    assert applicant.assessment_answers == {"q1": ["A"]}
    assert applicant.assessment_deadline > datetime.now(UTC)
    assert admin_session.scalars(
        select(AuditEvent).where(AuditEvent.entity_id == str(applicant.id)).where(AuditEvent.action == "applicant.access_extended")
    ).one()

def test_application_export_optional_invitation_columns_and_filters(app_client, admin_session, tenant_a):
    _seed_user(admin_session, tenant_a, "export-admin@a.edu", "admin")
    invited = admissions.submit_application(
        admin_session,
        tenant_id=tenant_a.id,
        email="invited-export@a.edu",
        first_name="Invited",
        last_name="Export",
        applied_on=date(2026, 2, 1),
    )
    invited.assessment_deadline = datetime(2026, 2, 8, tzinfo=UTC)
    other = admissions.submit_application(
        admin_session,
        tenant_id=tenant_a.id,
        email="other-export@a.edu",
        first_name="Other",
        last_name="Export",
        applied_on=date(2026, 3, 1),
    )
    admin_session.add(
        EmailOutbox(
            tenant_id=tenant_a.id,
            idempotency_key="export-invite:test",
            kind="entrance_invite",
            recipient=invited.email,
            subject="Invite",
            status="sent",
            sent_at=datetime(2026, 2, 2, tzinfo=UTC),
            html_body="html",
            text_body="text",
        )
    )
    admin_session.commit()

    response = app_client.get(
        "/admin/applications/export.csv?applied_from=2026-02-01&applied_to=2026-02-28&include_invitation=true",
        headers=_login(app_client, "export-admin@a.edu"),
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["content-disposition"] == "attachment; filename=applications.csv"
    body = response.text
    assert "Invitation Status" in body
    assert "Invitation Sent Date" in body
    assert "Invitation Expiry" in body
    assert "invited-export@a.edu" in body
    assert "other-export@a.edu" not in body
    assert "sent" in body
    assert other.id

def test_application_export_cannot_be_captured_by_applicant_detail_route():
    source = Path("app/web/applications.py").read_text()
    export_route = '@router.get("/export.csv")'
    detail_route = '@router.get("/{applicant_id:uuid}", response_class=HTMLResponse)'

    assert source.index(export_route) < source.index(detail_route)

def test_application_export_requires_admin(app_client, admin_session, tenant_a):
    _seed_user(admin_session, tenant_a, "export-student@a.edu", "student")
    response = app_client.get("/admin/applications/export.csv", headers=_login(app_client, "export-student@a.edu"))
    assert response.status_code == 403

def test_application_action_requires_csrf_after_cookie(app_client, admin_session, tenant_a):
    _seed_user(admin_session, tenant_a, "csrf-admin@a.edu", "admin")
    applicant = admissions.submit_application(
        admin_session,
        tenant_id=tenant_a.id,
        email="csrf-candidate@a.edu",
        first_name="Csrf",
        last_name="Candidate",
    )
    admin_session.commit()
    headers = _login(app_client, "csrf-admin@a.edu")
    app_client.get(f"/admin/applications/{applicant.id}", headers=headers)

    response = app_client.post(
        f"/admin/applications/{applicant.id}/action",
        headers=headers,
        data={"action": "extend_access"},
        follow_redirects=False,
    )

    assert response.status_code == 403
