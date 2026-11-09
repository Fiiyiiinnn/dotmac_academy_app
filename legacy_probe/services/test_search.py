"""Service-layer tests for app.services.search."""

from __future__ import annotations

from dotmac_kernel.models import PartyRoleGrant

from app.models.cohort import Cohort, Enrollment
from app.models.course import Chapter, Course
from app.models.offering import CourseOffering
from app.services.bootstrap import ensure_roles
from app.services.search import search
from app.services.security import hash_password
from legacy_probe.conftest import make_person_credential, make_person_party


def _person(admin_session, tenant, email):
    p = make_person_party(admin_session, tenant_id=tenant.id, email=email, first_name="S", last_name="U")
    admin_session.add(p)
    admin_session.flush()
    admin_session.add(
        make_person_credential(admin_session, tenant_id=tenant.id, party_id=p.id, email=email, password_hash=hash_password("pw"))
    )
    admin_session.flush()
    return p

def _course(admin_session, tenant, slug, title, chapter_title="Ch1", chapter_body="<p>intro content</p>"):
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
            body_html=chapter_body,
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
    admin_session.flush()

def _cleanup(admin_session, tenant):
    admin_session.rollback()
    from sqlalchemy import delete

    admin_session.execute(delete(Course).where(Course.tenant_id == tenant.id))
    admin_session.execute(delete(Cohort).where(Cohort.tenant_id == tenant.id))
    admin_session.commit()

# ── blank / whitespace ──────────────────────────────────────────────────────────

def test_blank_q_returns_empty(admin_session, tenant_a):
    p = _person(admin_session, tenant_a, "srch_blank@a.edu")
    admin_session.commit()
    try:
        result = search(admin_session, tenant_id=tenant_a.id, person_id=p.id, q="", is_staff=False)
        assert result == {"courses": [], "chapters": []}
        result2 = search(admin_session, tenant_id=tenant_a.id, person_id=p.id, q="   ", is_staff=False)
        assert result2 == {"courses": [], "chapters": []}
    finally:
        _cleanup(admin_session, tenant_a)

# ── accessible course hit ───────────────────────────────────────────────────────

def test_student_finds_course_title_hit(admin_session, tenant_a):
    p = _person(admin_session, tenant_a, "srch_stu1@a.edu")
    c = _course(admin_session, tenant_a, "srch-c1", "UniqueSearchTitle")
    _enroll(admin_session, tenant_a, p, c)
    admin_session.commit()
    try:
        result = search(admin_session, tenant_id=tenant_a.id, person_id=p.id, q="UniqueSearchTitle", is_staff=False)
        slugs = [r["slug"] for r in result["courses"]]
        assert "srch-c1" in slugs
    finally:
        _cleanup(admin_session, tenant_a)

def test_student_finds_chapter_title_hit(admin_session, tenant_a):
    p = _person(admin_session, tenant_a, "srch_stu2@a.edu")
    c = _course(admin_session, tenant_a, "srch-c2", "Some Course", chapter_title="DistinctChapterName")
    _enroll(admin_session, tenant_a, p, c)
    admin_session.commit()
    try:
        result = search(admin_session, tenant_id=tenant_a.id, person_id=p.id, q="DistinctChapterName", is_staff=False)
        assert len(result["chapters"]) >= 1
        assert result["chapters"][0]["course_slug"] == "srch-c2"
        assert result["chapters"][0]["chapter_number"] == 1
    finally:
        _cleanup(admin_session, tenant_a)

def test_student_finds_chapter_body_hit(admin_session, tenant_a):
    p = _person(admin_session, tenant_a, "srch_stu3@a.edu")
    c = _course(
        admin_session, tenant_a, "srch-c3", "Body Search Course", chapter_body="<p>xyzBodyKeyword content here</p>"
    )
    _enroll(admin_session, tenant_a, p, c)
    admin_session.commit()
    try:
        result = search(admin_session, tenant_id=tenant_a.id, person_id=p.id, q="xyzBodyKeyword", is_staff=False)
        assert len(result["chapters"]) >= 1
        assert result["chapters"][0]["course_slug"] == "srch-c3"
    finally:
        _cleanup(admin_session, tenant_a)

# ── non-accessible course excluded for student, returned for staff ──────────────

def test_non_accessible_excluded_for_student(admin_session, tenant_a):
    p = _person(admin_session, tenant_a, "srch_excl@a.edu")
    # course exists but student is NOT enrolled
    _course(admin_session, tenant_a, "srch-excl", "ExcludedCourseTitle")
    admin_session.commit()
    try:
        result = search(admin_session, tenant_id=tenant_a.id, person_id=p.id, q="ExcludedCourseTitle", is_staff=False)
        assert result["courses"] == []
        assert result["chapters"] == []
    finally:
        _cleanup(admin_session, tenant_a)

def test_non_accessible_returned_for_staff(admin_session, tenant_a):
    roles = ensure_roles(admin_session, tenant_a.id)
    p = _person(admin_session, tenant_a, "srch_staff@a.edu")
    admin_session.add(PartyRoleGrant(tenant_id=tenant_a.id, party_id=p.id, role_id=roles["instructor"].id))
    # course exists but staff is NOT enrolled — should still see it
    _course(admin_session, tenant_a, "srch-staff", "StaffOnlyCourseTitle")
    admin_session.commit()
    try:
        result = search(admin_session, tenant_id=tenant_a.id, person_id=p.id, q="StaffOnlyCourseTitle", is_staff=True)
        slugs = [r["slug"] for r in result["courses"]]
        assert "srch-staff" in slugs
    finally:
        _cleanup(admin_session, tenant_a)

# ── tenant isolation ────────────────────────────────────────────────────────────

def test_tenant_isolation(admin_session, tenant_a, tenant_b):
    p_a = _person(admin_session, tenant_a, "srch_iso_a@a.edu")
    # course belongs to tenant_b only
    _course(admin_session, tenant_b, "srch-iso-b", "IsolatedTenantCourse")
    admin_session.commit()
    try:
        result = search(admin_session, tenant_id=tenant_a.id, person_id=p_a.id, q="IsolatedTenantCourse", is_staff=True)
        assert result["courses"] == []
        assert result["chapters"] == []
    finally:
        _cleanup(admin_session, tenant_a)
        _cleanup(admin_session, tenant_b)
