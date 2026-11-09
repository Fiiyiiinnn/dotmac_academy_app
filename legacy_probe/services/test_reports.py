"""Tests for the reports service — cohort progress matrix + student transcript."""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.models.assessment import Activity, Score, Submission
from app.models.cohort import Cohort, Enrollment
from app.models.course import Course
from app.models.offering import CourseOffering
from app.services.exceptions import NotFoundError
from app.services.reports import cohort_matrix, student_transcript
from legacy_probe.conftest import make_person_party


def _seed(db, tid):
    """Course (networking) + 2 activities + cohort with 2 enrolled students + scores.

    Student A: passes ch1 (1.0), fails ch2-lab (0.4)  -> completion 1/2 = 0.5
    Student B: passes both (1.0, 0.8)                  -> completion 2/2 = 1.0
    """
    c = Course(tenant_id=tid, slug="net", title="Networking", discipline="networking",
               source_ref="x", version=1, status="published", listed=False)
    db.add(c)
    db.flush()

    a1 = Activity(tenant_id=tid, course_id=c.id, chapter_number=1, type="mcq_test",
                  title="Ch1 Test", pass_threshold=0.6, grading="auto", weight=1.0)
    a2 = Activity(tenant_id=tid, course_id=c.id, chapter_number=2, type="lab",
                  title="Ch2 Lab", pass_threshold=0.6, grading="auto", weight=1.0)
    db.add_all([a1, a2])
    db.flush()

    coh = Cohort(tenant_id=tid, name="Abuja 2026", discipline="networking", status="active")
    db.add(coh)
    db.flush()

    stu_a = make_person_party(db, tenant_id=tid, email="a@stu.edu", first_name="Aaa", last_name="Student")
    stu_b = make_person_party(db, tenant_id=tid, email="b@stu.edu", first_name="Bbb", last_name="Student")
    db.add_all([stu_a, stu_b])
    db.flush()

    for p in (stu_a, stu_b):
        db.add(Enrollment(tenant_id=tid, cohort_id=coh.id, person_id=p.id,
                          role_in_cohort="student", status="active"))
    db.add(CourseOffering(tenant_id=tid, cohort_id=coh.id, course_id=c.id, status="active"))
    db.flush()

    def _score(person, activity, frac, passed):
        sub = Submission(tenant_id=tid, activity_id=activity.id, person_id=person.id,
                         answers={}, attempt_no=1)
        db.add(sub)
        db.flush()
        db.add(Score(tenant_id=tid, submission_id=sub.id, score=frac * 10, max_score=10,
                     fraction=frac, passed=passed, per_item=[], source="auto"))
        db.flush()

    _score(stu_a, a1, 1.0, True)
    _score(stu_a, a2, 0.4, False)
    _score(stu_b, a1, 1.0, True)
    _score(stu_b, a2, 0.8, True)
    return c, coh, a1, a2, stu_a, stu_b

def test_cohort_matrix_shape_and_best(admin_session, tenant_a):
    c, coh, a1, a2, stu_a, stu_b = _seed(admin_session, tenant_a.id)
    m = cohort_matrix(admin_session, tenant_id=tenant_a.id, cohort_id=coh.id)

    # Activities ordered by chapter_number then type.
    assert [a.id for a in m["activities"]] == [a1.id, a2.id]
    assert len(m["rows"]) == 2

    rows = {r["email"]: r for r in m["rows"]}
    ra, rb = rows["a@stu.edu"], rows["b@stu.edu"]

    assert ra["cells"][a1.id].fraction == 1.0
    assert ra["cells"][a2.id].fraction == 0.4
    assert ra["completion"] == 0.5
    assert rb["completion"] == 1.0
    admin_session.rollback()

def test_cohort_matrix_uses_best_score(admin_session, tenant_a):
    """A later, lower-scoring submission must not override the best."""
    c, coh, a1, a2, stu_a, stu_b = _seed(admin_session, tenant_a.id)
    # Add a worse, later attempt for student A on a1.
    sub = Submission(tenant_id=tenant_a.id, activity_id=a1.id, person_id=stu_a.id,
                     answers={}, attempt_no=2)
    admin_session.add(sub)
    admin_session.flush()
    admin_session.add(Score(tenant_id=tenant_a.id, submission_id=sub.id, score=0, max_score=10,
                            fraction=0.0, passed=False, per_item=[], source="auto"))
    admin_session.flush()

    m = cohort_matrix(admin_session, tenant_id=tenant_a.id, cohort_id=coh.id)
    rows = {r["email"]: r for r in m["rows"]}
    assert rows["a@stu.edu"]["cells"][a1.id].fraction == 1.0
    admin_session.rollback()

def test_cohort_matrix_scoped_to_offerings_not_discipline(admin_session, tenant_a):
    """Finding #2: a same-discipline course with no offering is excluded from the matrix."""
    c, coh, a1, a2, stu_a, stu_b = _seed(admin_session, tenant_a.id)
    # A second networking course the cohort is NOT offered.
    other = Course(tenant_id=tenant_a.id, slug="other", title="Other Networking",
                   discipline="networking", source_ref="x", version=1, status="published", listed=False)
    admin_session.add(other)
    admin_session.flush()
    other_act = Activity(tenant_id=tenant_a.id, course_id=other.id, chapter_number=1,
                         type="mcq_test", title="Other Ch1", pass_threshold=0.6, grading="auto", weight=1.0)
    admin_session.add(other_act)
    admin_session.flush()

    m = cohort_matrix(admin_session, tenant_id=tenant_a.id, cohort_id=coh.id)
    ids = [a.id for a in m["activities"]]
    assert other_act.id not in ids
    assert ids == [a1.id, a2.id]
    admin_session.rollback()

def test_cohort_matrix_unknown_cohort_raises(admin_session, tenant_a):
    with pytest.raises(NotFoundError):
        cohort_matrix(admin_session, tenant_id=tenant_a.id, cohort_id=uuid4())
    admin_session.rollback()

def test_student_transcript(admin_session, tenant_a):
    c, coh, a1, a2, stu_a, stu_b = _seed(admin_session, tenant_a.id)
    t = student_transcript(admin_session, tenant_id=tenant_a.id, person_id=stu_a.id)
    assert t["person"].id == stu_a.id
    by_act = {r["activity"].id: r for r in t["rows"]}
    assert by_act[a1.id]["score"].passed is True
    assert by_act[a2.id]["score"].fraction == 0.4
    admin_session.rollback()
