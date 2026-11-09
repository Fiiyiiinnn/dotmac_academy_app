"""Public landing + catalog: the anonymous web presence (ADR 0003).

``Course.listed`` is the single selector for the public projection; these
tests pin the contract: anonymous ``/`` renders the landing (not a /login
redirect), ``/courses`` shows exactly the listed published courses, and the
signed-in ``/`` behaviour is unchanged.
"""

from __future__ import annotations

from sqlalchemy import select

from app.models.course import Course
from app.models.email_outbox import EmailOutbox
from legacy_probe.conftest import csrf_for, make_person_credential, make_person_party

H = {"Host": "alpha.localhost"}

def _seed_courses(admin_session, tid):
    listed = Course(
        tenant_id=tid, slug="fiber-basics", title="Fiber Basics",
        discipline="networking", source_ref="t@1", listed=True,
        status="published",
    )
    unlisted_internal = Course(
        tenant_id=tid, slug="instructor-guide", title="Instructor Guide",
        discipline="teaching", source_ref="t@1", listed=False,
        status="published",
    )
    listed_mgmt = Course(
        tenant_id=tid, slug="mgmt-team-leadership", title="Leading a Team",
        discipline="management", source_ref="t@1", listed=True,
        status="published",
    )
    unlisted_mgmt = Course(
        tenant_id=tid, slug="mgmt-vendor-procurement", title="Vendor Course Hidden",
        discipline="management", source_ref="t@1", listed=False,
        status="published",
    )
    draft_listed = Course(
        tenant_id=tid, slug="draft-course", title="Draft Course",
        discipline="networking", source_ref="t@1", listed=True, status="draft",
    )
    admin_session.add_all([listed, unlisted_internal, listed_mgmt, unlisted_mgmt, draft_listed])
    admin_session.commit()

def test_anonymous_root_renders_landing_not_login_redirect(app_client, admin_session, tenant_a):
    _seed_courses(admin_session, tenant_a.id)
    r = app_client.get("/", headers=H, follow_redirects=False)
    assert r.status_code == 200
    assert "Browse the courses" in r.text

def test_public_courses_shows_only_listed_published(app_client, admin_session, tenant_a):
    _seed_courses(admin_session, tenant_a.id)
    r = app_client.get("/courses", headers=H)
    assert r.status_code == 200
    assert "Fiber Basics" in r.text                 # listed technical
    assert "Leading a Team" in r.text               # listed management (grouped)
    assert r.text.index("Fiber Basics") < r.text.index("Leading a Team")  # tech group first
    assert "Instructor Guide" not in r.text         # unlisted internal
    assert "Vendor Course Hidden" not in r.text     # unlisted management
    assert "Draft Course" not in r.text             # listed but not published
    assert 'href="/management-enrollment" class="btn btn-mint">Enroll — Management</a>' in r.text
    assert "Enrol — Management" not in r.text
    assert "Enrolment" not in r.text
    assert "enrolment" not in r.text

def test_management_enrollment_form_renders_management_courses(app_client, admin_session, tenant_a):
    _seed_courses(admin_session, tenant_a.id)
    r = app_client.get("/management-enrollment", headers=H)
    assert r.status_code == 200
    assert 'hx-post="/management-enrollment"' in r.text
    assert "Leading a Team" in r.text
    assert "Fiber Basics" not in r.text

def test_management_enrollment_post_queues_inquiry(app_client, admin_session, tenant_a):
    csrf = csrf_for(app_client, H, "/management-enrollment")
    r = app_client.post(
        "/management-enrollment",
        headers={**H, "x-csrf-token": csrf},
        data={
            "first_name": "Morgan",
            "last_name": "Lee",
            "email": "MORGAN@example.com",
            "phone": "0800",
            "learner_type": "Team",
            "course_interest": "Leading a Team",
            "message": "Need seats for supervisors.",
        },
    )
    assert r.status_code == 200
    assert "Inquiry received" in r.text

    row = admin_session.scalars(
        select(EmailOutbox)
        .where(EmailOutbox.tenant_id == tenant_a.id)
        .where(EmailOutbox.kind == "management_inquiry")
    ).one()
    assert row.recipient == "fiberacademy@dotmac.ng"
    assert row.payload["email"] == "morgan@example.com"
    assert row.payload["learner_type"] == "Team"
    assert "Need seats for supervisors." in row.text_body

def test_signed_in_root_still_learn_home(app_client, admin_session, tenant_a):

    from app.services.security import hash_password

    _seed_courses(admin_session, tenant_a.id)
    p = make_person_party(admin_session, tenant_id=tenant_a.id, email="pl@alpha.edu", first_name="Pat", last_name="Lu")
    admin_session.add(p)
    admin_session.flush()
    admin_session.add(make_person_credential(admin_session, tenant_id=tenant_a.id, party_id=p.id, email="pl@alpha.edu", password_hash=hash_password("password1")))
    admin_session.commit()

    app_client.get("/login", headers=H)
    csrf = app_client.cookies.get("csrf_token", "")
    r = app_client.post(
        "/login",
        data={"email": "pl@alpha.edu", "password": "password1"},
        headers={**H, "x-csrf-token": csrf},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "session" in r.cookies
    r = app_client.get("/", headers=H, follow_redirects=False)
    assert r.status_code == 200
    # Learn Home, not the public landing.
    assert "Browse the courses" not in r.text
