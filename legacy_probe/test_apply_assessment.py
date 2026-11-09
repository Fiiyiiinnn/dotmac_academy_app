"""Public entrance-assessment flow: apply → tokenised exam → graded profile."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models.admissions import Applicant
from app.models.assessment import Question, QuestionBank
from app.models.cohort import Cohort
from app.models.course import Course
from app.models.track import CohortTrack, Track
from legacy_probe.conftest import client_for, csrf_for


def _cohort_with_exam(admin_session, tenant):
    admin_session.rollback()
    course = Course(
        tenant_id=tenant.id,
        slug=f"c-{uuid.uuid4().hex[:6]}",
        title="Intake",
        discipline="fiber",
        source_ref="x",
        version=1,
        status="published", listed=False,
    )
    admin_session.add(course)
    admin_session.commit()
    bank = QuestionBank(tenant_id=tenant.id, course_id=course.id, chapter_number=1, kind="chapter", version=1)
    admin_session.add(bank)
    admin_session.commit()
    for ext, cat in [("q1", "numeracy"), ("q2", "safety")]:
        admin_session.add(
            Question(
                tenant_id=tenant.id,
                bank_id=bank.id,
                ext_id=ext,
                stem="Pick A",
                type="single",
                options=["A", "B", "C", "D"],
                correct=["A"],
                rubric_category="recall",
                category=cat,
                explanation="",
                weight=1,
            )
        )
    cohort = Cohort(
        tenant_id=tenant.id,
        name="FA-Intake",
        discipline="fiber",
        status="active",
        entrance_bank_id=bank.id,
    )
    admin_session.add(cohort)
    admin_session.flush()
    track = Track(
        tenant_id=tenant.id,
        slug=f"fiber-{uuid.uuid4().hex[:6]}",
        name="Fiber Installer",
        status="active",
    )
    admin_session.add(track)
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
    admin_session.refresh(cohort)
    admin_session.refresh(track)
    return cohort, track


def test_apply_offers_assessment_and_grades_profile(app_client, tenant_a, admin_session):
    cohort, track = _cohort_with_exam(admin_session, tenant_a)
    a = client_for(TestClient(app_client.app), tenant_a.slug)

    r = a.post(
        "/apply",
        data={
            "first_name": "Cand",
            "last_name": "Idate",
            "email": "cand@a.ex",
            "track_choice": f"{cohort.id}:{track.id}",
        },
        headers={"x-csrf-token": csrf_for(a, {}, "/apply")},
    )
    assert r.status_code == 200, r.text
    assert "Start the assessment" in r.text
    m = re.search(r"/apply/assessment\?token=([A-Za-z0-9_-]+)", r.text)
    assert m, r.text
    token = m.group(1)

    # First open: the instruction screen — the clock has NOT started.
    page = a.get(f"/apply/assessment?token={token}")
    assert page.status_code == 200
    assert "Before you begin" in page.text
    assert "Pick A" not in page.text

    csrf = a.cookies.get("csrf_token", "")
    started = a.post(
        "/apply/assessment/start",
        data={"token": token},
        headers={"x-csrf-token": csrf},
        follow_redirects=True,
    )
    assert started.status_code == 200
    assert "Pick A" in started.text
    sub = a.post(
        "/apply/assessment",
        data={"token": token, "q1": "A", "q2": "B"},  # q1 right, q2 wrong
        headers={"x-csrf-token": csrf},
    )
    assert sub.status_code == 200, sub.text
    assert "Assessment submitted" in sub.text

    admin_session.rollback()
    applicant = admin_session.scalars(select(Applicant).where(Applicant.email == "cand@a.ex")).first()
    assert applicant is not None
    assert applicant.assessment_taken_at is not None
    assert applicant.assessment_score == 0.5
    assert applicant.assessment_profile == {"numeracy": 1.0, "safety": 0.0}


def test_assessment_heartbeat_uses_server_clock(app_client, tenant_a, admin_session):
    cohort, track = _cohort_with_exam(admin_session, tenant_a)
    cohort.entrance_time_limit_minutes = 30
    admin_session.commit()
    a = client_for(TestClient(app_client.app), tenant_a.slug)

    r = a.post(
        "/apply",
        data={
            "first_name": "Timer",
            "last_name": "Case",
            "email": "timer@a.ex",
            "track_choice": f"{cohort.id}:{track.id}",
        },
        headers={"x-csrf-token": csrf_for(a, {}, "/apply")},
    )
    token = re.search(r"/apply/assessment\?token=([A-Za-z0-9_-]+)", r.text).group(1)

    csrf0 = a.cookies.get("csrf_token", "")
    page = a.post(
        "/apply/assessment/start",
        data={"token": token},
        headers={"x-csrf-token": csrf0},
        follow_redirects=True,
    )
    assert 'data-remaining="1800"' in page.text

    # Move the authoritative server start back one minute; browser form fields
    # cannot alter elapsed time.
    admin_session.rollback()
    applicant = admin_session.scalars(
        select(Applicant).where(Applicant.email == "timer@a.ex")
    ).one()
    applicant.assessment_started_at = datetime.now(UTC) - timedelta(seconds=60)
    admin_session.commit()

    csrf = a.cookies.get("csrf_token", "")
    save = a.post(
        "/apply/assessment/save",
        data={"token": token, "assessment_elapsed_seconds": "1", "q1": "A"},
        headers={"x-csrf-token": csrf},
    )
    assert save.status_code == 204

    resumed = a.get(f"/apply/assessment?token={token}")
    remaining = int(re.search(r'data-remaining="(\d+)"', resumed.text).group(1))
    assert 1737 <= remaining <= 1740
    assert re.search(r'value="A"[^>]*checked', resumed.text)

    admin_session.rollback()
    applicant = admin_session.scalars(select(Applicant).where(Applicant.email == "timer@a.ex")).first()
    assert 60 <= applicant.assessment_elapsed_seconds <= 63
    assert applicant.assessment_answers == {"q1": ["A"]}


def test_bad_token_shows_notice(app_client, tenant_a):
    a = client_for(TestClient(app_client.app), tenant_a.slug)
    r = a.get("/apply/assessment?token=not-a-real-token")
    assert r.status_code == 200
    assert "Link not valid" in r.text


def test_apply_requires_an_open_canonical_track(app_client, tenant_a):
    a = client_for(TestClient(app_client.app), tenant_a.slug)
    r = a.post(
        "/apply",
        data={"first_name": "No", "last_name": "Exam", "email": "noexam@a.ex"},
        headers={"x-csrf-token": csrf_for(a, {}, "/apply")},
    )
    assert r.status_code == 422
