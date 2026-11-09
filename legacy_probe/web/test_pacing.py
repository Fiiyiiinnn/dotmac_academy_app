"""Per-activity release/due pacing enforcement (Slice 2b)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models.assessment import Activity, Question, QuestionBank
from app.models.cohort import Cohort, Enrollment
from app.models.course import Chapter, Course
from app.models.offering import CourseOffering
from app.models.pacing import OfferingActivity
from app.services.security import hash_password
from legacy_probe.conftest import grant_role, make_person_credential, make_person_party, web_login

H = {"Host": "alpha.localhost"}

def _setup(admin_session, tenant, email="pace@a.edu"):
    """Logged-in entitled learner with one mcq activity. Returns (person, course, activity, offering)."""
    p = make_person_party(admin_session, tenant_id=tenant.id, email=email, first_name="Pa", last_name="Cer")
    admin_session.add(p)
    admin_session.flush()
    admin_session.add(make_person_credential(admin_session, tenant_id=tenant.id, party_id=p.id, email=email, password_hash=hash_password("password1")))
    grant_role(admin_session, tenant_id=tenant.id, party_id=p.id)
    c = Course(tenant_id=tenant.id, slug="pace", title="Pace", discipline="networking",
               source_ref="x", version=1, status="published", listed=False)
    admin_session.add(c)
    admin_session.flush()
    admin_session.add(Chapter(tenant_id=tenant.id, course_id=c.id, number=1, title="One",
                              part="I", body_html="<p>b</p>", source_hash="h", order_index=1))
    bank = QuestionBank(tenant_id=tenant.id, course_id=c.id, chapter_number=1, kind="chapter", version=1)
    admin_session.add(bank)
    admin_session.flush()
    admin_session.add(Question(tenant_id=tenant.id, bank_id=bank.id, ext_id="q1", stem="Pick A",
                               type="single", options=["A", "B"], correct=["A"],
                               rubric_category="recall", explanation="", weight=1))
    act = Activity(tenant_id=tenant.id, course_id=c.id, chapter_number=1, type="mcq_test",
                   bank_id=bank.id, title="Pace Ch1", pass_threshold=0.6, grading="auto", weight=1.0)
    admin_session.add(act)
    coh = Cohort(tenant_id=tenant.id, name="C", discipline="networking", status="active")
    admin_session.add(coh)
    admin_session.flush()
    admin_session.add(Enrollment(tenant_id=tenant.id, cohort_id=coh.id, person_id=p.id,
                                 role_in_cohort="student", status="active"))
    off = CourseOffering(tenant_id=tenant.id, cohort_id=coh.id, course_id=c.id, status="active")
    admin_session.add(off)
    admin_session.flush()
    app_client_login = (p, c, act, off)
    return app_client_login

def _login(app_client, email="pace@a.edu"):
    web_login(app_client, H, email, "password1")

def _cleanup(admin_session, tenant):
    admin_session.query(Course).filter(Course.tenant_id == tenant.id).delete()
    admin_session.query(Cohort).filter(Cohort.tenant_id == tenant.id).delete()
    admin_session.commit()

def test_future_release_blocks_read_and_submit(app_client, admin_session, tenant_a):
    p, c, act, off = _setup(admin_session, tenant_a)
    admin_session.add(OfferingActivity(tenant_id=tenant_a.id, offering_id=off.id, activity_id=act.id,
                                       release_at=datetime.now(UTC) + timedelta(days=2)))
    admin_session.commit()
    _login(app_client)
    try:
        assert app_client.get(f"/activities/{act.id}", headers=H).status_code == 403
        # chapter page sets csrf even if activity blocked
        app_client.get("/courses/pace/chapters/1", headers=H)
        csrf = app_client.cookies.get("csrf_token", "")
        r = app_client.post(f"/activities/{act.id}/submit",
                            headers={**H, "x-csrf-token": csrf}, data={"q1": "A"})
        assert r.status_code == 403
    finally:
        _cleanup(admin_session, tenant_a)

def test_past_due_blocks_submit_but_allows_read(app_client, admin_session, tenant_a):
    p, c, act, off = _setup(admin_session, tenant_a)
    admin_session.add(OfferingActivity(
        tenant_id=tenant_a.id, offering_id=off.id, activity_id=act.id,
        release_at=datetime.now(UTC) - timedelta(days=2),
        due_at=datetime.now(UTC) - timedelta(days=1)))
    admin_session.commit()
    _login(app_client)
    try:
        assert app_client.get(f"/activities/{act.id}", headers=H).status_code == 200
        csrf = app_client.cookies.get("csrf_token", "")
        r = app_client.post(f"/activities/{act.id}/submit",
                            headers={**H, "x-csrf-token": csrf}, data={"q1": "A"})
        assert r.status_code == 403
    finally:
        _cleanup(admin_session, tenant_a)

def test_open_window_allows_read_and_submit(app_client, admin_session, tenant_a):
    p, c, act, off = _setup(admin_session, tenant_a)
    admin_session.add(OfferingActivity(
        tenant_id=tenant_a.id, offering_id=off.id, activity_id=act.id,
        release_at=datetime.now(UTC) - timedelta(days=1),
        due_at=datetime.now(UTC) + timedelta(days=1)))
    admin_session.commit()
    _login(app_client)
    try:
        assert app_client.get(f"/activities/{act.id}", headers=H).status_code == 200
        csrf = app_client.cookies.get("csrf_token", "")
        r = app_client.post(f"/activities/{act.id}/submit",
                            headers={**H, "x-csrf-token": csrf}, data={"q1": "A"})
        assert r.status_code == 200
    finally:
        _cleanup(admin_session, tenant_a)

def test_no_override_is_unrestricted(app_client, admin_session, tenant_a):
    p, c, act, off = _setup(admin_session, tenant_a)
    admin_session.commit()  # no OfferingActivity row
    _login(app_client)
    try:
        assert app_client.get(f"/activities/{act.id}", headers=H).status_code == 200
        csrf = app_client.cookies.get("csrf_token", "")
        r = app_client.post(f"/activities/{act.id}/submit",
                            headers={**H, "x-csrf-token": csrf}, data={"q1": "A"})
        assert r.status_code == 200
    finally:
        _cleanup(admin_session, tenant_a)
