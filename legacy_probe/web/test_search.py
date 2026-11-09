"""Web tests for GET /search."""

from __future__ import annotations

from dotmac_kernel.models import PartyRoleGrant

from app.models.cohort import Cohort, Enrollment
from app.models.course import Chapter, Course
from app.models.offering import CourseOffering
from app.services.bootstrap import ensure_roles
from app.services.security import hash_password
from legacy_probe.conftest import grant_role, make_person_credential, make_person_party, web_login

H = {"Host": "alpha.localhost"}

def _login(app_client, admin_session, tenant, email):
    p = make_person_party(admin_session, tenant_id=tenant.id, email=email, first_name="W", last_name="U")
    admin_session.add(p)
    admin_session.flush()
    admin_session.add(
        make_person_credential(admin_session, tenant_id=tenant.id, party_id=p.id, email=email, password_hash=hash_password("pw"))
    )
    grant_role(admin_session, tenant_id=tenant.id, party_id=p.id, role="student")
    admin_session.commit()
    web_login(app_client, H, email, "pw")
    return p

def _login_staff(app_client, admin_session, tenant, email):
    roles = ensure_roles(admin_session, tenant.id)
    p = make_person_party(admin_session, tenant_id=tenant.id, email=email, first_name="Ins", last_name="U")
    admin_session.add(p)
    admin_session.flush()
    admin_session.add(
        make_person_credential(admin_session, tenant_id=tenant.id, party_id=p.id, email=email, password_hash=hash_password("pw"))
    )
    admin_session.add(PartyRoleGrant(tenant_id=tenant.id, party_id=p.id, role_id=roles["instructor"].id))
    admin_session.commit()
    web_login(app_client, H, email, "pw")
    return p

def _course(admin_session, tenant, slug, title, chapter_title="Ch1"):
    c = Course(
        tenant_id=tenant.id,
        slug=slug,
        title=title,
        discipline="net",
        source_ref="x",
        version=1,
        status="published",
        listed=False,
    )
    admin_session.add(c)
    admin_session.flush()
    admin_session.add(
        Chapter(
            tenant_id=tenant.id,
            course_id=c.id,
            number=1,
            title=chapter_title,
            part="I",
            body_html="<p>body</p>",
            source_hash="h",
            order_index=1,
        )
    )
    admin_session.flush()
    return c

def _enroll(admin_session, tenant, person, course):
    coh = Cohort(tenant_id=tenant.id, name="Coh", discipline="net", status="active")
    admin_session.add(coh)
    admin_session.flush()
    admin_session.add(
        Enrollment(
            tenant_id=tenant.id, cohort_id=coh.id, person_id=person.id, role_in_cohort="student", status="active"
        )
    )
    admin_session.add(CourseOffering(tenant_id=tenant.id, cohort_id=coh.id, course_id=course.id, status="active"))
    admin_session.commit()

def _cleanup(admin_session, tenant):
    admin_session.rollback()
    from sqlalchemy import delete

    # Mapped tables resolve their schema (the Learning tables live in mod_learning).
    admin_session.execute(delete(Course.__table__).where(Course.__table__.c.tenant_id == tenant.id))
    admin_session.execute(delete(Cohort.__table__).where(Cohort.__table__.c.tenant_id == tenant.id))
    admin_session.commit()

def test_search_requires_login(app_client, admin_session, tenant_a):
    r = app_client.get("/search?q=anything", headers=H, follow_redirects=False)
    assert r.status_code in (302, 303)

def test_search_blank_returns_empty_state(app_client, admin_session, tenant_a):
    _login(app_client, admin_session, tenant_a, "wsrch_blank@a.edu")
    admin_session.commit()
    r = app_client.get("/search?q=", headers=H)
    assert r.status_code == 200
    # empty state text appears
    assert "No results" in r.text or "Enter a search" in r.text or r.status_code == 200

def test_search_hit_in_accessible_course(app_client, admin_session, tenant_a):
    p = _login(app_client, admin_session, tenant_a, "wsrch_acc@a.edu")
    c = _course(admin_session, tenant_a, "wsrch-acc", "WebAccessibleCourse")
    _enroll(admin_session, tenant_a, p, c)
    try:
        r = app_client.get("/search?q=WebAccessibleCourse", headers=H)
        assert r.status_code == 200
        assert "WebAccessibleCourse" in r.text
        assert "/courses/wsrch-acc" in r.text
    finally:
        _cleanup(admin_session, tenant_a)

def test_search_excludes_non_accessible_for_student(app_client, admin_session, tenant_a):
    _login(app_client, admin_session, tenant_a, "wsrch_excl@a.edu")
    _course(admin_session, tenant_a, "wsrch-excl", "WebExcludedCourse")
    admin_session.commit()
    try:
        r = app_client.get("/search?q=WebExcludedCourse", headers=H)
        assert r.status_code == 200
        # The course link must not appear (q appears in input value, so check for the link)
        assert "/courses/wsrch-excl" not in r.text
    finally:
        _cleanup(admin_session, tenant_a)

def test_search_staff_sees_non_accessible_course(app_client, admin_session, tenant_a):
    _login_staff(app_client, admin_session, tenant_a, "wsrch_staff@a.edu")
    _course(admin_session, tenant_a, "wsrch-staff", "WebStaffOnlyCourse")
    admin_session.commit()
    try:
        r = app_client.get("/search?q=WebStaffOnlyCourse", headers=H)
        assert r.status_code == 200
        assert "WebStaffOnlyCourse" in r.text
    finally:
        _cleanup(admin_session, tenant_a)

def test_search_chapter_link_present(app_client, admin_session, tenant_a):
    p = _login(app_client, admin_session, tenant_a, "wsrch_ch@a.edu")
    c = _course(admin_session, tenant_a, "wsrch-ch", "Chapter Link Course", chapter_title="DistinctChapterWebTitle")
    _enroll(admin_session, tenant_a, p, c)
    try:
        r = app_client.get("/search?q=DistinctChapterWebTitle", headers=H)
        assert r.status_code == 200
        assert "/courses/wsrch-ch/chapters/1" in r.text
    finally:
        _cleanup(admin_session, tenant_a)
