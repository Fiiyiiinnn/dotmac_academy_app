"""Tests for the instructor lab monitor + override — Task 10."""

from __future__ import annotations

from datetime import UTC, datetime

from dotmac_kernel.models import Party, PartyPerson, PartyRoleGrant, PartyType, UserCredential

from app.models.assessment import Activity, Score, Submission
from app.models.identity_extensions import AcademyAccountProfile, AcademyCredentialPolicy
from app.services.bootstrap import ensure_roles
from app.services.security import hash_password
from legacy_probe.conftest import web_login


def _make_account(admin_session, tenant, email: str, first_name: str, last_name: str, role: str) -> Party:
    """Genuine canonical account (Party/PartyPerson/profile/credential/policy/grant)."""
    roles = ensure_roles(admin_session, tenant.id)
    now = datetime.now(UTC)
    p = Party(
        tenant_id=tenant.id, party_type=PartyType.person,
        display_name=f"{first_name} {last_name}", email=email, is_active=True,
    )
    admin_session.add(p)
    admin_session.flush()
    admin_session.add(PartyPerson(party_id=p.id, first_name=first_name, last_name=last_name))
    admin_session.flush()
    admin_session.add(AcademyAccountProfile(
        tenant_id=tenant.id, party_id=p.id, status="active", prefs={},
        created_at=now, updated_at=now,
    ))
    credential = UserCredential(
        tenant_id=tenant.id, party_id=p.id, password_hash=hash_password("password1"),
    )
    admin_session.add(credential)
    admin_session.flush()
    admin_session.add(AcademyCredentialPolicy(
        tenant_id=tenant.id, credential_id=credential.id, email=email,
        failed_login_attempts=0, locked_until=None, lifecycle_version=0,
        reset_required=False, created_at=now, updated_at=now,
    ))
    admin_session.add(PartyRoleGrant(tenant_id=tenant.id, party_id=p.id, role_id=roles[role].id))
    admin_session.commit()
    return p


def _login_instructor(app_client, admin_session, tenant):
    """Seed an instructor person, log in via TestClient, return Host header dict."""
    _make_account(admin_session, tenant, "li@a.edu", "Lab", "Instr", "instructor")

    h = {"Host": "alpha.localhost"}
    web_login(app_client, h, "li@a.edu", "password1")
    return h


def test_student_forbidden_on_monitor(app_client, admin_session, tenant_a):
    """A user with only the student role gets 403 on GET /instructor/labs."""
    _make_account(admin_session, tenant_a, "ls@a.edu", "S", "T", "student")

    h = {"Host": "alpha.localhost"}
    web_login(app_client, h, "ls@a.edu", "password1")
    assert app_client.get("/instructor/labs", headers=h).status_code == 403


def test_instructor_override_creates_override_score(app_client, admin_session, tenant_a):
    """An instructor override on a seeded lab Submission creates a Score(source='override')."""
    h = _login_instructor(app_client, admin_session, tenant_a)

    # Seed a real course + lab activity + a student submission to override.
    from app.models.course import Course

    student = Party(tenant_id=tenant_a.id, party_type=PartyType.person, display_name="L N",
                    email="learner@a.edu", is_active=True)
    course = Course(tenant_id=tenant_a.id, slug="lab", title="Lab", discipline="networking",
                    source_ref="x", version=1, status="published", listed=False)
    admin_session.add_all([student, course])
    admin_session.flush()
    admin_session.add(PartyPerson(party_id=student.id, first_name="L", last_name="N"))
    admin_session.flush()
    activity = Activity(
        tenant_id=tenant_a.id, course_id=course.id, type="lab", title="VLAN Lab",
        pass_threshold=0.7, grading="auto", weight=1.0,
    )
    admin_session.add(activity)
    admin_session.flush()
    sub = Submission(
        tenant_id=tenant_a.id, activity_id=activity.id, person_id=student.id,
        answers={"seed": {"o": 5}, "instance": "dal-x"},
    )
    admin_session.add(sub)
    admin_session.commit()
    admin_session.refresh(sub)

    csrf = app_client.cookies.get("csrf_token", "")
    r = app_client.post(
        f"/instructor/labs/scores/{sub.id}/override",
        headers={**h, "x-csrf-token": csrf},
        data={"score_value": "8", "max_score": "10", "reason": "manual lab review"},
        follow_redirects=False,
    )
    assert r.status_code in (200, 303)

    override = (
        admin_session.query(Score)
        .filter(Score.submission_id == sub.id, Score.source == "override")
        .one_or_none()
    )
    assert override is not None
    assert override.score == 8.0
    assert override.max_score == 10.0
    assert override.override_reason == "manual lab review"
