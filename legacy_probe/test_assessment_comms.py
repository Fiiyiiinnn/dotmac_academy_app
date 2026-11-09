"""Assessment communications: intro screen, outcome emails, certificate email.

Requires a migrated disposable Postgres (skipped otherwise by the fixtures).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import settings
from app.models.admissions import Applicant
from app.models.email_outbox import EmailOutbox
from app.services.localtime import to_local
from legacy_probe.conftest import client_for, csrf_for, make_person_party
from legacy_probe.test_apply_assessment import _cohort_with_exam


def _apply(app_client, tenant, cohort, track, email):
    a = client_for(TestClient(app_client.app), tenant.slug)
    r = a.post(
        "/apply",
        data={
            "first_name": "Com",
            "last_name": "Ms",
            "email": email,
            "track_choice": f"{cohort.id}:{track.id}",
        },
        headers={"x-csrf-token": csrf_for(a, {}, "/apply")},
    )
    token = re.search(r"/apply/assessment\?token=([A-Za-z0-9_-]+)", r.text).group(1)
    return a, token

def _start(client, token):
    csrf = client.cookies.get("csrf_token", "")
    return client.post(
        "/apply/assessment/start",
        data={"token": token},
        headers={"x-csrf-token": csrf},
        follow_redirects=True,
    )

def test_intro_screen_shown_and_clock_not_started(app_client, tenant_a, admin_session):
    cohort, track = _cohort_with_exam(admin_session, tenant_a)
    a, token = _apply(app_client, tenant_a, cohort, track, "intro@a.ex")

    page = a.get(f"/apply/assessment?token={token}")
    assert "Before you begin" in page.text
    assert "Start my assessment" in page.text
    assert "Pick A" not in page.text

    admin_session.rollback()
    applicant = admin_session.scalars(select(Applicant).where(Applicant.email == "intro@a.ex")).first()
    assert applicant.assessment_started_at is None  # curious click costs nothing

    started = _start(a, token)
    assert "Pick A" in started.text
    admin_session.rollback()
    admin_session.refresh(applicant)
    assert applicant.assessment_started_at is not None

    # Reopening after start resumes the questions, not the intro.
    again = a.get(f"/apply/assessment?token={token}")
    assert "Pick A" in again.text
    assert "Before you begin" not in again.text

def test_exam_invite_shows_full_local_expiry_timestamp(app_client, tenant_a, admin_session):
    cohort, track = _cohort_with_exam(admin_session, tenant_a)
    _apply(app_client, tenant_a, cohort, track, "deadline-email@a.ex")

    admin_session.rollback()
    applicant = admin_session.scalars(
        select(Applicant).where(Applicant.email == "deadline-email@a.ex")
    ).one()
    invitation = admin_session.scalars(
        select(EmailOutbox)
        .where(EmailOutbox.recipient == applicant.email)
        .where(EmailOutbox.kind == "entrance_invite")
    ).one()
    local_deadline = to_local(applicant.assessment_deadline)
    assert local_deadline is not None
    expected = (
        f"{local_deadline.strftime('%A %d %B %Y at %H:%M')} "
        f"{local_deadline.tzname()} ({settings.academy_timezone})"
    )
    assert f"Your assessment link is valid until {expected}." in invitation.html_body
    assert f"Your assessment link is valid until {expected}." in invitation.text_body

def test_expired_deadline_blocks_autosave_and_submission(app_client, tenant_a, admin_session):
    cohort, track = _cohort_with_exam(admin_session, tenant_a)
    a, token = _apply(app_client, tenant_a, cohort, track, "expired-write@a.ex")
    _start(a, token)

    admin_session.rollback()
    applicant = admin_session.scalars(
        select(Applicant).where(Applicant.email == "expired-write@a.ex")
    ).one()
    applicant.assessment_answers = {"q2": ["B"]}
    applicant.assessment_deadline = datetime.now(UTC) - timedelta(seconds=1)
    admin_session.commit()

    csrf = a.cookies.get("csrf_token", "")
    autosave = a.post(
        "/apply/assessment/save",
        data={"token": token, "q1": "A"},
        headers={"x-csrf-token": csrf},
    )
    assert autosave.status_code == 204
    submitted = a.post(
        "/apply/assessment",
        data={"token": token, "q1": "A"},
        headers={"x-csrf-token": csrf},
    )
    assert submitted.status_code == 200
    assert "This assessment has closed" in submitted.text

    admin_session.rollback()
    admin_session.refresh(applicant)
    assert applicant.assessment_answers == {"q2": ["B"]}
    assert applicant.assessment_taken_at is None

def test_waitlist_outcome_email(app_client, tenant_a, admin_session, monkeypatch):
    import app.services.applicant_email as ae

    cohort, track = _cohort_with_exam(admin_session, tenant_a)
    cohort.auto_accept_threshold = 0.9
    admin_session.commit()
    a, token = _apply(app_client, tenant_a, cohort, track, "wl@a.ex")
    _start(a, token)

    admin_session.rollback()
    applicant = admin_session.scalars(
        select(Applicant).where(Applicant.email == "wl@a.ex")
    ).one()
    applicant.assessment_started_at = datetime.now(UTC) - timedelta(minutes=10)
    admin_session.commit()

    sent: list[str] = []
    # Record the call but run the real sender: the assessment context requires the keyed outbox row.
    real_waitlist, real_received = ae.send_waitlist_notice, ae.send_results_received

    def recording_waitlist(db, *, applicant):
        sent.append("waitlist")
        return real_waitlist(db, applicant=applicant)

    def recording_received(db, *, applicant):
        sent.append("received")
        return real_received(db, applicant=applicant)

    monkeypatch.setattr(ae, "send_waitlist_notice", recording_waitlist)
    monkeypatch.setattr(ae, "send_results_received", recording_received)

    csrf = a.cookies.get("csrf_token", "")
    r = a.post(
        "/apply/assessment",
        data={"token": token, "q1": "A", "q2": "B"},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 200

    admin_session.rollback()
    applicant = admin_session.scalars(select(Applicant).where(Applicant.email == "wl@a.ex")).first()
    assert applicant.status == "waitlisted"  # 0.5 valid, below 0.9
    assert sent == ["waitlist"]
    assert admin_session.scalars(
        select(EmailOutbox).where(
            EmailOutbox.tenant_id == tenant_a.id,
            EmailOutbox.kind == "assessment_waitlist",
            EmailOutbox.recipient == "wl@a.ex",
        )
    ).one()

def test_invalid_sitting_gets_received_email(app_client, tenant_a, admin_session, monkeypatch):
    import app.services.applicant_email as ae

    cohort, track = _cohort_with_exam(admin_session, tenant_a)
    cohort.auto_accept_threshold = 0.4
    admin_session.commit()
    a, token = _apply(app_client, tenant_a, cohort, track, "inv2@a.ex")
    _start(a, token)

    sent: list[str] = []
    # Record the call but run the real sender: the assessment context requires the keyed outbox row.
    real_waitlist, real_received = ae.send_waitlist_notice, ae.send_results_received

    def recording_waitlist(db, *, applicant):
        sent.append("waitlist")
        return real_waitlist(db, applicant=applicant)

    def recording_received(db, *, applicant):
        sent.append("received")
        return real_received(db, applicant=applicant)

    monkeypatch.setattr(ae, "send_waitlist_notice", recording_waitlist)
    monkeypatch.setattr(ae, "send_results_received", recording_received)

    csrf = a.cookies.get("csrf_token", "")
    # Submit with ~zero elapsed: score 0.5 clears 0.4 but the sitting is too
    # fast to be valid, so the policy holds it and the neutral email goes out.
    r = a.post(
        "/apply/assessment",
        data={"token": token, "q1": "A", "q2": "B"},
        headers={"x-csrf-token": csrf},
    )
    assert r.status_code == 200

    admin_session.rollback()
    applicant = admin_session.scalars(select(Applicant).where(Applicant.email == "inv2@a.ex")).first()
    assert applicant.status == "applied"
    assert applicant.assessment_valid is False
    assert sent == ["received"]
    assert admin_session.scalars(
        select(EmailOutbox).where(
            EmailOutbox.tenant_id == tenant_a.id,
            EmailOutbox.kind == "assessment_review",
            EmailOutbox.recipient == "inv2@a.ex",
        )
    ).one()

def test_certificate_emailed_on_completion(app_client, tenant_a, admin_session, monkeypatch):
    from app.models.completion import CourseCompletion
    from app.models.course import Course
    from app.services import completion as completion_svc
    from app.services import email_outbox as outbox_svc
    from app.services.email import EmailResult

    admin_session.rollback()
    course = Course(
        tenant_id=tenant_a.id,
        slug="cert-course",
        title="Cert Course",
        discipline="fiber",
        source_ref="x",
        version=1,
        status="published", listed=False,
    )
    person = make_person_party(admin_session, tenant_id=tenant_a.id, email="grad@a.ex", first_name="Gra", last_name="Duate")
    admin_session.add_all([course, person])
    admin_session.commit()
    rec = CourseCompletion(
        tenant_id=tenant_a.id,
        person_id=person.id,
        course_id=course.id,
        status="completed",
        pct=1.0,
    )
    admin_session.add(rec)
    admin_session.commit()

    captured: dict = {}

    def fake_send(
        to,
        subject,
        html_body,
        text_body=None,
        db=None,
        attachments=None,
        message_id=None,
    ):
        captured.update(
            to=to,
            subject=subject,
            attachments=attachments,
            message_id=message_id,
        )
        return EmailResult(True)

    monkeypatch.setattr(outbox_svc, "send_email_detailed", fake_send)

    # Issue a real certificate through its owner; the email carries that fact.
    from app.services.certificates import issue_certificate

    certificate = issue_certificate(admin_session, tenant_id=tenant_a.id, person_id=person.id, course_id=course.id)
    ok = completion_svc._email_certificate(
        admin_session,
        tenant_id=tenant_a.id,
        person_id=person.id,
        course_id=course.id,
        course_title="Cert Course",
        certificate=certificate,
    )
    assert ok
    admin_session.commit()
    row = admin_session.scalars(
        select(EmailOutbox)
        .where(EmailOutbox.tenant_id == tenant_a.id)
        .where(EmailOutbox.kind == "certificate")
    ).one()
    assert row.status == "pending"

    result = outbox_svc.deliver_pending(admin_session)
    admin_session.commit()
    assert result["sent"] == 1
    assert captured["to"] == "grad@a.ex"
    assert "Cert Course" in captured["subject"]
    filename, data, mime = captured["attachments"][0]
    assert filename == "certificate.pdf"
    assert mime == "application/pdf"
    assert data[:4] == b"%PDF"
    assert captured["message_id"].startswith("<academy-outbox-")
