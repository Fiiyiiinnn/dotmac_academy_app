"""Course prerequisite enforcement (Slice 2e)."""

from __future__ import annotations

from datetime import UTC, datetime
from html import unescape

from app.models.assessment import Activity
from app.models.cohort import Cohort, Enrollment
from app.models.completion import CourseCompletion
from app.models.course import Course
from app.models.offering import CourseOffering
from app.models.prerequisite import CoursePrerequisite
from app.services.entitlements import course_access_states
from app.services.security import hash_password
from app.services.tracks import create_cohort_track
from legacy_probe.conftest import grant_role, make_person_credential, make_person_party, web_login

H = {"Host": "alpha.localhost"}

def _course(admin_session, tid, slug):
    c = Course(tenant_id=tid, slug=slug, title=slug.title(), discipline="networking",
               source_ref="x", version=1, status="published", listed=False)
    admin_session.add(c)
    admin_session.flush()
    act = Activity(tenant_id=tid, course_id=c.id, chapter_number=1, type="mcq_test",
                   title=f"{slug} act", pass_threshold=0.6, grading="auto", weight=1.0)
    admin_session.add(act)
    admin_session.flush()
    return c, act

def _setup(admin_session, tenant):
    """Learner entitled to course B which requires course A. Returns (person, A, B, actB)."""
    p = make_person_party(admin_session, tenant_id=tenant.id, email="pre@a.edu", first_name="Pre", last_name="Req")
    admin_session.add(p)
    admin_session.flush()
    admin_session.add(make_person_credential(admin_session, tenant_id=tenant.id, party_id=p.id, email="pre@a.edu", password_hash=hash_password("password1")))
    grant_role(admin_session, tenant_id=tenant.id, party_id=p.id)
    a, _ = _course(admin_session, tenant.id, "alpha")
    a.title = "Fiber Fundamentals"
    b, act_b = _course(admin_session, tenant.id, "beta")
    coh = Cohort(tenant_id=tenant.id, name="C", discipline="networking", status="active")
    admin_session.add(coh)
    admin_session.flush()
    admin_session.add(Enrollment(tenant_id=tenant.id, cohort_id=coh.id, person_id=p.id,
                                 role_in_cohort="student", status="active"))
    # Entitle to both A and B so only the prerequisite gates B.
    admin_session.add(CourseOffering(tenant_id=tenant.id, cohort_id=coh.id, course_id=a.id, status="active"))
    admin_session.add(CourseOffering(tenant_id=tenant.id, cohort_id=coh.id, course_id=b.id, status="active"))
    admin_session.add(CoursePrerequisite(tenant_id=tenant.id, course_id=b.id, requires_course_id=a.id))
    admin_session.flush()
    return p, a, b, act_b

def _login(app_client):
    web_login(app_client, H, "pre@a.edu", "password1")

def _cleanup(admin_session, tenant):
    admin_session.query(Course).filter(Course.tenant_id == tenant.id).delete()
    admin_session.query(Cohort).filter(Cohort.tenant_id == tenant.id).delete()
    admin_session.commit()

def test_prerequisite_incomplete_blocks_access(app_client, admin_session, tenant_a):
    p, a, b, act_b = _setup(admin_session, tenant_a)
    admin_session.commit()  # A not completed
    states = course_access_states(admin_session, tenant_id=tenant_a.id, person_id=p.id)
    assert states[b.id].locked is True
    assert states[b.id].locked_until_course_id == a.id
    assert states[b.id].locked_reason == 'Complete "Fiber Fundamentals" to unlock this course.'
    _login(app_client)
    try:
        assert app_client.get(f"/activities/{act_b.id}", headers=H).status_code == 403
        courses_page = app_client.get("/courses", headers=H)
        assert courses_page.status_code == 200
        rendered = unescape(courses_page.text)
        assert 'Complete "Fiber Fundamentals" to unlock this course.' in rendered
        assert 'title="Complete &#34;Fiber Fundamentals&#34; to unlock this course."' in courses_page.text
        assert (
            'aria-label="Locked: Complete &#34;Fiber Fundamentals&#34; to unlock this course."'
            in courses_page.text
        )
    finally:
        _cleanup(admin_session, tenant_a)

def test_prerequisite_completed_allows_access(app_client, admin_session, tenant_a):
    p, a, b, act_b = _setup(admin_session, tenant_a)
    admin_session.add(CourseCompletion(tenant_id=tenant_a.id, person_id=p.id, course_id=a.id,
                                       status="completed", pct=1.0, completed_at=datetime.now(UTC)))
    admin_session.commit()
    states = course_access_states(admin_session, tenant_id=tenant_a.id, person_id=p.id)
    assert states[b.id].locked is False
    _login(app_client)
    try:
        assert app_client.get(f"/activities/{act_b.id}", headers=H).status_code == 200
    finally:
        _cleanup(admin_session, tenant_a)

def test_track_order_lock_reason_names_previous_course(admin_session, tenant_a):
    person = make_person_party(admin_session, tenant_id=tenant_a.id, email="track@a.edu", first_name="Track", last_name="Learner")
    admin_session.add(person)
    first, _ = _course(admin_session, tenant_a.id, "fiber-fundamentals")
    first.title = "Fiber Fundamentals"
    second, _ = _course(admin_session, tenant_a.id, "fiber-advanced")
    cohort = Cohort(tenant_id=tenant_a.id, name="Track Cohort", discipline="networking", status="active")
    admin_session.add(cohort)
    admin_session.flush()
    track = create_cohort_track(
        admin_session, tenant_id=tenant_a.id, cohort_id=cohort.id,
        name="Fiber Track", course_ids=[first.id, second.id],
    )
    admin_session.add(Enrollment(
        tenant_id=tenant_a.id, cohort_id=cohort.id, person_id=person.id, track_id=track.id,
        role_in_cohort="student", status="active",
    ))
    admin_session.commit()

    states = course_access_states(admin_session, tenant_id=tenant_a.id, person_id=person.id)
    assert states[second.id].locked is True
    assert states[second.id].locked_until_course_id == first.id
    assert states[second.id].locked_reason == 'Complete "Fiber Fundamentals" to unlock this course.'
