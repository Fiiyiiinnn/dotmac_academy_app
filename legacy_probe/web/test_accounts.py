"""Tests for the account-creation web flow (/admin/users)."""

from __future__ import annotations

from dotmac_kernel.models import Party, PartyRoleGrant

from app.models.auth import UserCredential
from app.models.rbac import Role
from app.services.bootstrap import ensure_roles
from app.services.security import hash_password
from legacy_probe.conftest import make_person_credential, make_person_party, web_login


def _seed_user(admin_session, tenant, email, role_slug):
    """Seed a Person + credential (password 'password1') + the given role."""
    roles = ensure_roles(admin_session, tenant.id)
    p = make_person_party(admin_session, tenant_id=tenant.id, email=email, first_name="Seed", last_name="User")
    admin_session.add(p)
    admin_session.flush()
    admin_session.add(
        make_person_credential(admin_session, tenant_id=tenant.id, party_id=p.id, email=email, password_hash=hash_password("password1"))
    )
    admin_session.add(PartyRoleGrant(tenant_id=tenant.id, party_id=p.id, role_id=roles[role_slug].id))
    admin_session.commit()
    return p

def _login(app_client, email, password="password1"):
    """Log in and return (headers, csrf_token)."""
    h = {"Host": "alpha.localhost"}
    web_login(app_client, h, email, password)
    csrf = app_client.cookies.get("csrf_token", "")
    return h, csrf

def test_student_forbidden_on_users_page(app_client, admin_session, tenant_a):
    _seed_user(admin_session, tenant_a, "stud@a.edu", "student")
    h, _ = _login(app_client, "stud@a.edu")
    assert app_client.get("/admin/users", headers=h).status_code == 403

def test_instructor_can_list_users(app_client, admin_session, tenant_a):
    _seed_user(admin_session, tenant_a, "inst@a.edu", "instructor")
    h, _ = _login(app_client, "inst@a.edu")
    r = app_client.get("/admin/users", headers=h)
    assert r.status_code == 200
    assert "inst@a.edu" in r.text

def test_instructor_can_create_student(app_client, admin_session, tenant_a):
    _seed_user(admin_session, tenant_a, "inst2@a.edu", "instructor")
    h, csrf = _login(app_client, "inst2@a.edu")

    r = app_client.post(
        "/admin/users",
        headers={**h, "x-csrf-token": csrf, "HX-Request": "true"},
        data={
            "first_name": "New",
            "last_name": "Stud",
            "email": "created-stud@a.edu",
            "password": "password1",
            "role": "student",
        },
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert r.headers.get("HX-Redirect") == "/admin/users"

    person = (
        admin_session.query(Party).filter(Party.tenant_id == tenant_a.id, Party.email == "created-stud@a.edu").one()
    )
    cred = (
        admin_session.query(UserCredential)
        .filter(UserCredential.tenant_id == tenant_a.id, UserCredential.party_id == person.id)
        .one()
    )
    assert cred is not None
    student_role = admin_session.query(Role).filter(Role.tenant_id == tenant_a.id, Role.slug == "student").one()
    grant = (
        admin_session.query(PartyRoleGrant)
        .filter(PartyRoleGrant.tenant_id == tenant_a.id, PartyRoleGrant.party_id == person.id)
        .one()
    )
    assert grant.role_id == student_role.id

def test_instructor_cannot_create_admin(app_client, admin_session, tenant_a):
    _seed_user(admin_session, tenant_a, "inst3@a.edu", "instructor")
    h, csrf = _login(app_client, "inst3@a.edu")

    r = app_client.post(
        "/admin/users",
        headers={**h, "x-csrf-token": csrf, "HX-Request": "true"},
        data={
            "first_name": "Bad",
            "last_name": "Admin",
            "email": "no-admin@a.edu",
            "password": "password1",
            "role": "admin",
        },
        follow_redirects=False,
    )
    assert r.status_code == 403
    assert (
        admin_session.query(Party).filter(Party.tenant_id == tenant_a.id, Party.email == "no-admin@a.edu").count()
        == 0
    )

def test_admin_can_create_admin(app_client, admin_session, tenant_a):
    _seed_user(admin_session, tenant_a, "admin@a.edu", "admin")
    h, csrf = _login(app_client, "admin@a.edu")

    r = app_client.post(
        "/admin/users",
        headers={**h, "x-csrf-token": csrf, "HX-Request": "true"},
        data={
            "first_name": "Extra",
            "last_name": "Admin",
            "email": "extra-admin@a.edu",
            "password": "password1",
            "role": "admin",
        },
        follow_redirects=False,
    )
    assert r.status_code == 200
    admin_role = admin_session.query(Role).filter(Role.tenant_id == tenant_a.id, Role.slug == "admin").one()
    person = (
        admin_session.query(Party).filter(Party.tenant_id == tenant_a.id, Party.email == "extra-admin@a.edu").one()
    )
    grant = (
        admin_session.query(PartyRoleGrant)
        .filter(PartyRoleGrant.tenant_id == tenant_a.id, PartyRoleGrant.party_id == person.id)
        .one()
    )
    assert grant.role_id == admin_role.id

def test_created_student_can_login_and_reach_learner_page(app_client, admin_session, tenant_a):
    _seed_user(admin_session, tenant_a, "inst4@a.edu", "instructor")
    h, csrf = _login(app_client, "inst4@a.edu")
    app_client.post(
        "/admin/users",
        headers={**h, "x-csrf-token": csrf, "HX-Request": "true"},
        data={
            "first_name": "Login",
            "last_name": "Able",
            "email": "loginable@a.edu",
            "password": "s3cret-pass",
            "role": "student",
        },
        follow_redirects=False,
    )

    # Fresh client → the newly created student logs in and reaches the dashboard.
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as student_client:
        sh = {"Host": "alpha.localhost"}
        web_login(student_client, sh, "loginable@a.edu", "s3cret-pass")
        dash = student_client.get("/", headers=sh)
        assert dash.status_code == 200

def test_admin_can_search_users(app_client, admin_session, tenant_a):
    _seed_user(admin_session, tenant_a, "admin-search@a.edu", "admin")
    _seed_user(admin_session, tenant_a, "needle.user@a.edu", "student")
    _seed_user(admin_session, tenant_a, "other.user@a.edu", "student")
    h, _ = _login(app_client, "admin-search@a.edu")

    r = app_client.get("/admin/users?q=needle", headers=h)

    assert r.status_code == 200
    assert "needle.user@a.edu" in r.text
    assert "other.user@a.edu" not in r.text
    assert "Showing results for" in r.text

def test_admin_can_change_existing_user_role(app_client, admin_session, tenant_a):
    _seed_user(admin_session, tenant_a, "role-admin@a.edu", "admin")
    target = _seed_user(admin_session, tenant_a, "role-target@a.edu", "student")
    h, csrf = _login(app_client, "role-admin@a.edu")

    r = app_client.post(
        f"/admin/users/{target.id}/role",
        headers={**h, "x-csrf-token": csrf, "HX-Request": "true"},
        data={"role": "instructor"},
        follow_redirects=False,
    )

    assert r.status_code == 200
    assert r.headers.get("HX-Redirect") == "/admin/users"
    grants = (
        admin_session.query(Role.slug)
        .join(
            PartyRoleGrant,
            (PartyRoleGrant.role_id == Role.id) & (PartyRoleGrant.tenant_id == Role.tenant_id),
        )
        .filter(PartyRoleGrant.tenant_id == tenant_a.id, PartyRoleGrant.party_id == target.id)
        .order_by(Role.slug)
        .all()
    )
    assert [slug for (slug,) in grants] == ["instructor"]

def test_instructor_cannot_change_existing_user_role(app_client, admin_session, tenant_a):
    _seed_user(admin_session, tenant_a, "role-inst@a.edu", "instructor")
    target = _seed_user(admin_session, tenant_a, "role-student@a.edu", "student")
    h, csrf = _login(app_client, "role-inst@a.edu")

    r = app_client.post(
        f"/admin/users/{target.id}/role",
        headers={**h, "x-csrf-token": csrf, "HX-Request": "true"},
        data={"role": "admin"},
        follow_redirects=False,
    )

    assert r.status_code == 403
    grants = (
        admin_session.query(Role.slug)
        .join(
            PartyRoleGrant,
            (PartyRoleGrant.role_id == Role.id) & (PartyRoleGrant.tenant_id == Role.tenant_id),
        )
        .filter(PartyRoleGrant.tenant_id == tenant_a.id, PartyRoleGrant.party_id == target.id)
        .order_by(Role.slug)
        .all()
    )
    assert [slug for (slug,) in grants] == ["student"]

def test_admin_invite_assigns_selected_track(app_client, admin_session, tenant_a):
    from sqlalchemy import select

    from app.models.cohort import Cohort, Enrollment
    from app.models.course import Course
    from app.models.track import Track
    from app.services.tracks import create_cohort_track

    _seed_user(admin_session, tenant_a, "track-admin@a.edu", "admin")
    cohort = Cohort(tenant_id=tenant_a.id, name="Track Cohort", discipline="networking", status="active")
    course = Course(
        tenant_id=tenant_a.id,
        slug="track-invite-course",
        title="Track Invite Course",
        discipline="networking",
        source_ref="test",
        version=1,
        status="published",
        listed=False,
    )
    admin_session.add_all([cohort, course])
    admin_session.flush()
    track = create_cohort_track(
        admin_session, tenant_id=tenant_a.id, cohort_id=cohort.id, name="Invite Track", course_ids=[course.id]
    )
    admin_session.commit()
    h, csrf = _login(app_client, "track-admin@a.edu")

    r = app_client.post(
        "/admin/users/invite",
        headers={**h, "x-csrf-token": csrf, "HX-Request": "true"},
        data={
            "first_name": "Track",
            "last_name": "Student",
            "email": "track-student@a.edu",
            "role": "student",
            "cohort_ids": [str(cohort.id)],
            "track_ids": [f"{cohort.id}:{track.id}"],
        },
    )

    assert r.status_code == 200
    assert "Invite Track track in Track Cohort" in r.text
    invited = admin_session.scalars(
        select(Party).where(Party.tenant_id == tenant_a.id).where(Party.email == "track-student@a.edu")
    ).first()
    enrollment = admin_session.scalars(
        select(Enrollment)
        .where(Enrollment.tenant_id == tenant_a.id)
        .where(Enrollment.cohort_id == cohort.id)
        .where(Enrollment.person_id == invited.id)
    ).first()
    assert enrollment.track_id == track.id
    assert admin_session.scalars(select(Track).where(Track.id == track.id)).first().name == "Invite Track"
