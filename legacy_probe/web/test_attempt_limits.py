"""Assessment attempt limits (Slice 4a, finding #4)."""

from __future__ import annotations

from app.models.assessment import Activity, Question, QuestionBank, Submission
from app.models.cohort import Cohort, Enrollment
from app.models.course import Chapter, Course
from app.models.offering import CourseOffering
from app.services.security import hash_password
from legacy_probe.conftest import grant_role, make_person_credential, make_person_party, web_login

H = {"Host": "alpha.localhost"}

def _setup(admin_session, tenant, *, max_attempts):
    p = make_person_party(admin_session, tenant_id=tenant.id, email="att@a.edu", first_name="At", last_name="T")
    admin_session.add(p)
    admin_session.flush()
    admin_session.add(make_person_credential(admin_session, tenant_id=tenant.id, party_id=p.id, email="att@a.edu", password_hash=hash_password("password1")))
    grant_role(admin_session, tenant_id=tenant.id, party_id=p.id, role="student")
    c = Course(tenant_id=tenant.id, slug="att", title="Att", discipline="networking",
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
                   bank_id=bank.id, title="Att Ch1", pass_threshold=0.6, max_attempts=max_attempts, grading="auto", weight=1.0)
    admin_session.add(act)
    coh = Cohort(tenant_id=tenant.id, name="C", discipline="networking", status="active")
    admin_session.add(coh)
    admin_session.flush()
    admin_session.add(Enrollment(tenant_id=tenant.id, cohort_id=coh.id, person_id=p.id,
                                 role_in_cohort="student", status="active"))
    admin_session.add(CourseOffering(tenant_id=tenant.id, cohort_id=coh.id, course_id=c.id, status="active"))
    admin_session.commit()
    return p, c, act

def _login(app_client):
    web_login(app_client, H, "att@a.edu", "password1")

def _cleanup(admin_session, tenant):
    admin_session.query(Course).filter(Course.tenant_id == tenant.id).delete()
    admin_session.query(Cohort).filter(Cohort.tenant_id == tenant.id).delete()
    admin_session.commit()

def _submit(app_client, act):
    app_client.get("/courses/att/chapters/1", headers=H)
    csrf = app_client.cookies.get("csrf_token", "")
    return app_client.post(f"/activities/{act.id}/submit",
                           headers={**H, "x-csrf-token": csrf}, data={"q1": "B"})  # wrong → fail

def test_attempts_capped(app_client, admin_session, tenant_a):
    p, c, act = _setup(admin_session, tenant_a, max_attempts=2)
    _login(app_client)
    try:
        assert _submit(app_client, act).status_code == 200
        assert _submit(app_client, act).status_code == 200
        # Third attempt exceeds the cap.
        assert _submit(app_client, act).status_code == 409
        n = admin_session.query(Submission).filter(Submission.activity_id == act.id).count()
        assert n == 2
    finally:
        _cleanup(admin_session, tenant_a)

def test_unlimited_when_null(app_client, admin_session, tenant_a):
    p, c, act = _setup(admin_session, tenant_a, max_attempts=None)
    _login(app_client)
    try:
        for _ in range(3):
            assert _submit(app_client, act).status_code == 200
    finally:
        _cleanup(admin_session, tenant_a)
