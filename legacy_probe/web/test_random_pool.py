"""Randomized question pools (Slice 4e, finding #4)."""

from __future__ import annotations

from app.models.assessment import Activity, Question, QuestionBank, Score, Submission
from app.models.attempt import ActivityAttempt
from app.models.cohort import Cohort, Enrollment
from app.models.course import Chapter, Course
from app.models.offering import CourseOffering
from app.services.security import hash_password
from legacy_probe.conftest import grant_role, make_person_credential, make_person_party, web_login

H = {"Host": "alpha.localhost"}

def _setup(admin_session, tenant, *, pool=2, total=5):
    p = make_person_party(admin_session, tenant_id=tenant.id, email="pool@a.edu", first_name="Po", last_name="Ol")
    admin_session.add(p)
    admin_session.flush()
    admin_session.add(make_person_credential(admin_session, tenant_id=tenant.id, party_id=p.id, email="pool@a.edu", password_hash=hash_password("password1")))
    grant_role(admin_session, tenant_id=tenant.id, party_id=p.id)
    c = Course(tenant_id=tenant.id, slug="pool", title="Pool", discipline="networking",
               source_ref="x", version=1, status="published", listed=False)
    admin_session.add(c)
    admin_session.flush()
    admin_session.add(Chapter(tenant_id=tenant.id, course_id=c.id, number=1, title="One",
                              part="I", body_html="<p>b</p>", source_hash="h", order_index=1))
    bank = QuestionBank(tenant_id=tenant.id, course_id=c.id, chapter_number=1, kind="chapter", version=1)
    admin_session.add(bank)
    admin_session.flush()
    for i in range(1, total + 1):
        admin_session.add(Question(tenant_id=tenant.id, bank_id=bank.id, ext_id=f"q{i}",
                                   stem=f"Pick A ({i})", type="single", options=["A", "B"],
                                   correct=["A"], rubric_category="recall", explanation="", weight=1))
    act = Activity(tenant_id=tenant.id, course_id=c.id, chapter_number=1, type="mcq_test",
                   bank_id=bank.id, title="Pool Quiz", pass_threshold=0.6, question_count=pool, grading="auto", weight=1.0)
    admin_session.add(act)
    coh = Cohort(tenant_id=tenant.id, name="C", discipline="networking", status="active")
    admin_session.add(coh)
    admin_session.flush()
    admin_session.add(Enrollment(tenant_id=tenant.id, cohort_id=coh.id, person_id=p.id,
                                 role_in_cohort="student", status="active"))
    admin_session.add(CourseOffering(tenant_id=tenant.id, cohort_id=coh.id, course_id=c.id, status="active"))
    admin_session.commit()
    return p, act, total

def _login(app_client):
    web_login(app_client, H, "pool@a.edu", "password1")

def _cleanup(admin_session, tenant):
    admin_session.query(Course).filter(Course.tenant_id == tenant.id).delete()
    admin_session.query(Cohort).filter(Cohort.tenant_id == tenant.id).delete()
    admin_session.commit()

def test_pool_shows_subset_and_grades_only_subset(app_client, admin_session, tenant_a):
    p, act, total = _setup(admin_session, tenant_a, pool=2, total=5)
    _login(app_client)
    try:
        # Opening the activity draws a 2-question attempt and renders only those.
        r = app_client.get(f"/activities/{act.id}", headers=H)
        assert r.status_code == 200
        assert r.text.count("<fieldset") == 2          # only 2 of 5 shown
        attempt = admin_session.query(ActivityAttempt).filter(
            ActivityAttempt.activity_id == act.id, ActivityAttempt.person_id == p.id).one()
        assert len(attempt.question_ext_ids) == 2
        assert attempt.submitted_at is None

        # Answer all 5 (form may include extras); grading counts only the attempt's 2.
        csrf = app_client.cookies.get("csrf_token", "")
        r2 = app_client.post(f"/activities/{act.id}/submit",
                             headers={**H, "x-csrf-token": csrf},
                             data={f"q{i}": "A" for i in range(1, total + 1)})
        assert r2.status_code == 200
        score = admin_session.query(Score).join(
            Submission, Score.submission_id == Submission.id).filter(
            Submission.activity_id == act.id, Submission.person_id == p.id).one()
        assert score.max_score == 2.0   # graded the 2-question subset, not all 5
        assert score.passed is True
        # Attempt is now closed.
        admin_session.refresh(attempt)
        assert attempt.submitted_at is not None
    finally:
        _cleanup(admin_session, tenant_a)

def test_null_pool_shows_all_questions(app_client, admin_session, tenant_a):
    """question_count=None preserves the show-the-whole-bank behaviour."""
    p, act, total = _setup(admin_session, tenant_a, pool=None, total=4)
    _login(app_client)
    try:
        r = app_client.get(f"/activities/{act.id}", headers=H)
        assert r.text.count("<fieldset") == 4
        assert admin_session.query(ActivityAttempt).filter(
            ActivityAttempt.activity_id == act.id).count() == 0  # no attempt created
    finally:
        _cleanup(admin_session, tenant_a)
