"""Cohort dashboard + at-risk flagging (Slice 5 / finding #9)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models.assessment import Activity, Score, Submission
from app.models.cohort import Cohort, Enrollment
from app.models.completion import CourseCompletion
from app.models.course import Course
from app.models.offering import CourseOffering
from app.services.dashboards import cohort_overview
from legacy_probe.conftest import make_person_party

NOW = datetime(2026, 6, 27, tzinfo=UTC)

def _enroll(db, tid, coh, person):
    db.add(Enrollment(tenant_id=tid, cohort_id=coh.id, person_id=person.id,
                      role_in_cohort="student", status="active"))

def _activity_score(db, tid, course, person, created_at):
    act = Activity(tenant_id=tid, course_id=course.id, chapter_number=1, type="mcq_test",
                   title="A", pass_threshold=0.6, grading="auto", weight=1.0)
    db.add(act)
    db.flush()
    sub = Submission(tenant_id=tid, activity_id=act.id, person_id=person.id, answers={}, attempt_no=1)
    db.add(sub)
    db.flush()
    s = Score(tenant_id=tid, submission_id=sub.id, score=10, max_score=10, fraction=1.0,
              passed=True, per_item=[], source="auto")
    s.created_at = created_at
    db.add(s)
    db.flush()

def test_cohort_overview_flags_at_risk(admin_session, tenant_a):
    tid = tenant_a.id
    course = Course(tenant_id=tid, slug="net", title="Net", discipline="networking",
                    source_ref="x", version=1, status="published", listed=False)
    coh = Cohort(tenant_id=tid, name="C", discipline="networking", status="active")
    admin_session.add_all([course, coh])
    admin_session.flush()
    admin_session.add(CourseOffering(tenant_id=tid, cohort_id=coh.id, course_id=course.id, status="active"))

    # Strong learner: full completion + recent activity → not at risk.
    strong = make_person_party(admin_session, tenant_id=tid, email="strong@a.edu", first_name="Str", last_name="Ong")
    # Weak learner: low completion + no activity → at risk.
    weak = make_person_party(admin_session, tenant_id=tid, email="weak@a.edu", first_name="Wea", last_name="K")
    admin_session.add_all([strong, weak])
    admin_session.flush()
    _enroll(admin_session, tid, coh, strong)
    _enroll(admin_session, tid, coh, weak)
    admin_session.add(CourseCompletion(tenant_id=tid, person_id=strong.id, course_id=course.id,
                                       status="completed", pct=1.0, completed_at=NOW))
    admin_session.add(CourseCompletion(tenant_id=tid, person_id=weak.id, course_id=course.id,
                                       status="in_progress", pct=0.2, completed_at=None))
    _activity_score(admin_session, tid, course, strong, NOW - timedelta(days=1))  # recent
    admin_session.flush()

    ov = cohort_overview(admin_session, tenant_id=tid, cohort_id=coh.id, now=NOW)
    by_email = {r["email"]: r for r in ov["rows"]}
    assert by_email["strong@a.edu"]["completion_pct"] == 1.0
    assert by_email["strong@a.edu"]["at_risk"] is False
    assert by_email["weak@a.edu"]["completion_pct"] == 0.2
    assert by_email["weak@a.edu"]["at_risk"] is True
    admin_session.rollback()
