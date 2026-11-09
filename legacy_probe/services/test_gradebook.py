"""Tests for the weighted gradebook service."""
from __future__ import annotations

import pytest
from dotmac_learning import course_grade

from app.models.assessment import Activity, Score, Submission
from app.models.cohort import Cohort, Enrollment
from app.models.course import Course
from app.models.offering import CourseOffering
from app.services.gradebook import cohort_gradebook
from legacy_probe.conftest import make_person_party


def _seed_course_with_activities(db, tid, *, weight_a=1.0, weight_b=3.0):
    course = Course(tenant_id=tid, slug="g-net", title="Gradebook Net", discipline="networking",
                    source_ref="x", version=1, status="published", listed=False)
    db.add(course)
    db.flush()
    a1 = Activity(tenant_id=tid, course_id=course.id, chapter_number=1, type="mcq_test",
                  title="Quiz A", pass_threshold=0.6, weight=weight_a, grading="auto")
    a2 = Activity(tenant_id=tid, course_id=course.id, chapter_number=2, type="mcq_test",
                  title="Quiz B", pass_threshold=0.6, weight=weight_b, grading="auto")
    db.add_all([a1, a2])
    db.flush()
    return course, a1, a2

def _add_score(db, tid, *, activity_id, person_id, fraction):
    sub = Submission(tenant_id=tid, activity_id=activity_id, person_id=person_id,
                     answers={}, attempt_no=1)
    db.add(sub)
    db.flush()
    score = Score(tenant_id=tid, submission_id=sub.id, score=fraction * 10, max_score=10,
                  fraction=fraction, passed=(fraction >= 0.6), per_item=[], source="auto")
    db.add(score)
    db.flush()

def test_weighted_average_25_percent(admin_session, tenant_a):
    """Weight 1 x fraction 1.0 + weight 3 x fraction 0.0 = 25%."""
    tid = tenant_a.id
    course, a1, a2 = _seed_course_with_activities(admin_session, tid, weight_a=1.0, weight_b=3.0)
    student = make_person_party(admin_session, tenant_id=tid, email="stu@g.edu", first_name="S", last_name="T")
    admin_session.add(student)
    admin_session.flush()
    _add_score(admin_session, tid, activity_id=a1.id, person_id=student.id, fraction=1.0)
    admin_session.commit()

    result = course_grade(admin_session, tenant_id=tid, person_id=student.id, course_id=course.id)
    assert result.pct == 25
    assert len(result.per_activity) == 2
    fractions = {item.activity.id: item.fraction for item in result.per_activity}
    assert fractions[a1.id] == 1.0
    assert fractions[a2.id] == 0.0

def test_missing_submission_counts_as_zero(admin_session, tenant_a):
    """No submission for an activity → fraction 0, weight still in denominator."""
    tid = tenant_a.id
    course, a1, a2 = _seed_course_with_activities(admin_session, tid, weight_a=2.0, weight_b=2.0)
    student = make_person_party(admin_session, tenant_id=tid, email="stu2@g.edu", first_name="S", last_name="U")
    admin_session.add(student)
    admin_session.flush()
    # Only score a1 (fraction=0.5), a2 has no submission
    _add_score(admin_session, tid, activity_id=a1.id, person_id=student.id, fraction=0.5)
    admin_session.commit()

    result = course_grade(admin_session, tenant_id=tid, person_id=student.id, course_id=course.id)
    # (0.5*2 + 0.0*2) / (2+2) = 1.0/4.0 = 0.25 → 25%
    assert result.pct == 25

def test_all_zero_weights_returns_zero(admin_session, tenant_a):
    """Total weight=0 guard → pct 0."""
    tid = tenant_a.id
    course, a1, a2 = _seed_course_with_activities(admin_session, tid, weight_a=0.0, weight_b=0.0)
    student = make_person_party(admin_session, tenant_id=tid, email="stu3@g.edu", first_name="S", last_name="V")
    admin_session.add(student)
    admin_session.flush()
    _add_score(admin_session, tid, activity_id=a1.id, person_id=student.id, fraction=1.0)
    admin_session.commit()

    result = course_grade(admin_session, tenant_id=tid, person_id=student.id, course_id=course.id)
    assert result.pct == 0

def test_no_activities_returns_zero(admin_session, tenant_a):
    """Empty course → pct 0, empty per_activity."""
    tid = tenant_a.id
    course = Course(tenant_id=tid, slug="g-empty", title="Empty", discipline="net",
                    source_ref="x", version=1, status="published", listed=False)
    admin_session.add(course)
    student = make_person_party(admin_session, tenant_id=tid, email="stu4@g.edu", first_name="S", last_name="W")
    admin_session.add(student)
    admin_session.commit()

    result = course_grade(admin_session, tenant_id=tid, person_id=student.id, course_id=course.id)
    assert result.pct == 0
    assert result.per_activity == ()

def test_cohort_gradebook_structure(admin_session, tenant_a):
    """cohort_gradebook returns correct keys and computes final_pct per student."""
    tid = tenant_a.id
    course, a1, a2 = _seed_course_with_activities(admin_session, tid, weight_a=1.0, weight_b=3.0)
    cohort = Cohort(tenant_id=tid, name="GB Cohort", discipline="networking", status="active")
    admin_session.add(cohort)
    admin_session.flush()
    admin_session.add(CourseOffering(tenant_id=tid, cohort_id=cohort.id, course_id=course.id, status="active"))

    stu = make_person_party(admin_session, tenant_id=tid, email="gb@stu.edu", first_name="G", last_name="B")
    admin_session.add(stu)
    admin_session.flush()
    admin_session.add(Enrollment(tenant_id=tid, cohort_id=cohort.id, person_id=stu.id,
                                 role_in_cohort="student", status="active"))
    admin_session.flush()
    # Score only a1 with fraction 1.0 → final = 25%
    _add_score(admin_session, tid, activity_id=a1.id, person_id=stu.id, fraction=1.0)
    admin_session.commit()

    gb = cohort_gradebook(admin_session, tenant_id=tid, cohort_id=cohort.id)
    assert gb["cohort"].id == cohort.id
    assert len(gb["activities"]) == 2
    assert len(gb["rows"]) == 1
    row = gb["rows"][0]
    assert row["final_pct"] == 25
    assert len(row["cells"]) == 2
    assert row["cells"][0]["pct"] == 100  # a1 fraction=1.0
    assert row["cells"][1]["pct"] == 0    # a2 missing

def test_cohort_gradebook_404_wrong_tenant(admin_session, tenant_a, tenant_b):
    """Cohort in tenant_b raises NotFoundError when queried as tenant_a."""
    from app.services.exceptions import NotFoundError

    cohort_b = Cohort(tenant_id=tenant_b.id, name="B Cohort", discipline="net", status="active")
    admin_session.add(cohort_b)
    admin_session.commit()
    admin_session.refresh(cohort_b)

    with pytest.raises(NotFoundError):
        cohort_gradebook(admin_session, tenant_id=tenant_a.id, cohort_id=cohort_b.id)
