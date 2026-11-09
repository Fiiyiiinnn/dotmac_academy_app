"""Public apply page (GET form + POST intake)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from legacy_probe.conftest import client_for, csrf_for


def _track_choice(admin_session, tenant):
    from app.models.assessment import QuestionBank
    from app.models.cohort import Cohort
    from app.models.course import Course
    from app.models.track import CohortTrack, Track

    course = Course(
        tenant_id=tenant.id,
        slug="intake-assessment",
        title="Intake Assessment",
        discipline="fiber",
        source_ref="test",
        version=1,
        status="published", listed=False,
    )
    admin_session.add(course)
    admin_session.flush()
    bank = QuestionBank(
        tenant_id=tenant.id,
        course_id=course.id,
        chapter_number=1,
        kind="chapter",
        version=1,
    )
    admin_session.add(bank)
    admin_session.flush()
    cohort = Cohort(
        tenant_id=tenant.id,
        name="Fiber Intake",
        discipline="fiber",
        status="active",
        entrance_bank_id=bank.id,
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
    return f"{cohort.id}:{track.id}"


def test_apply_form_renders(app_client, tenant_a, admin_session):
    _track_choice(admin_session, tenant_a)
    a = client_for(app_client, tenant_a.slug)
    r = a.get("/apply")
    assert r.status_code == 200
    assert 'hx-post="/apply"' in r.text
    assert "/static/csrf.js" in r.text  # the CSRF shim is present
    assert a.cookies.get("csrf_token")  # the safe GET issued a signed token
    assert "Fiber Installer" in r.text


def test_apply_post_creates_applicant(app_client, tenant_a, admin_session, api_actor):
    # A fresh client first GETs the form (which issues the signed CSRF token),
    # then posts that token, exercising the handler + service wiring.
    a = client_for(TestClient(app_client.app), tenant_a.slug)
    track_choice = _track_choice(admin_session, tenant_a)
    r = a.post(
        "/apply",
        data={
            "first_name": "Web",
            "last_name": "Applicant",
            "email": "web@a.ex",
            "phone": "0800",
            "track_choice": track_choice,
        },
        headers={"x-csrf-token": csrf_for(a, {}, "/apply")},
    )
    assert r.status_code == 200, r.text
    assert "Application received" in r.text
    # It landed in the admissions API too (same tenant).
    from legacy_probe.conftest import client_for as cf

    admin = cf(TestClient(app_client.app), tenant_a.slug)
    auth = api_actor(admin, tenant_a, email="adm2@a.ex")["headers"]
    listed = admin.get("/admissions", headers=auth).json()
    assert any(x["email"] == "web@a.ex" for x in listed)


def test_apply_post_escapes_name(app_client, tenant_a, admin_session):
    a = client_for(TestClient(app_client.app), tenant_a.slug)
    track_choice = _track_choice(admin_session, tenant_a)
    r = a.post(
        "/apply",
        data={
            "first_name": "<script>x</script>",
            "last_name": "T",
            "email": "xss@a.ex",
            "track_choice": track_choice,
        },
        headers={"x-csrf-token": csrf_for(a, {}, "/apply")},
    )
    assert r.status_code == 200
    assert "<script>x</script>" not in r.text  # escaped
    assert "&lt;script&gt;" in r.text
