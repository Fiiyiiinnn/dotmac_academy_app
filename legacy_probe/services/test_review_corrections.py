"""Review-correction regression that Academy owns: auto-graded results never raise a "graded" reminder.

The other four corrections (explicit-prerequisite lock, genuine-zero below-passing, severity order,
once-per-subject event ledger) are dotmac-learning behaviour covered by Starter tests/test_learning_entitlements.py,
tests/test_learning_success_queue.py and tests/test_learning_events.py at the locked wheel; see
docs/inventories/test_dispositions.toml.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models.assessment import Activity, Score, Submission
from app.models.cohort import Cohort, Enrollment
from app.models.course import Course
from app.models.offering import CourseOffering
from app.services import reminders
from legacy_probe.conftest import make_person_party


def _course(admin_session, tenant, slug):
    c = Course(tenant_id=tenant.id, slug=slug, title=slug.title(),
               discipline="fiber", source_ref="t@1", status="published", listed=False)
    admin_session.add(c)
    admin_session.flush()
    return c

def _enrol(admin_session, tenant, person, course, *, track_id=None, days_ago=1):
    cohort = Cohort(tenant_id=tenant.id, name=f"C {course.slug}", discipline="fiber", status="active")
    admin_session.add(cohort)
    admin_session.flush()
    enr = Enrollment(tenant_id=tenant.id, cohort_id=cohort.id, person_id=person.id,
                     role_in_cohort="student", status="active", track_id=track_id)
    admin_session.add(enr)
    off = CourseOffering(tenant_id=tenant.id, cohort_id=cohort.id, course_id=course.id, status="active")
    admin_session.add(off)
    admin_session.flush()
    enr.created_at = datetime.now(UTC) - timedelta(days=days_ago)
    admin_session.flush()
    return cohort

def test_graded_reminder_skips_auto_scores(admin_session, tenant_a):
    """Auto-graded quiz results must not generate 'graded' reminders."""
    person = make_person_party(admin_session, tenant_id=tenant_a.id, email="autg@a.edu", first_name="A", last_name="G")
    admin_session.add(person)
    course = _course(admin_session, tenant_a, "autog-course")
    _enrol(admin_session, tenant_a, person, course)
    act = Activity(tenant_id=tenant_a.id, course_id=course.id, chapter_number=1,
                   type="mcq_test", title="Quiz", pass_threshold=0.6, grading="auto", weight=1.0)
    admin_session.add(act)
    admin_session.flush()
    sub = Submission(tenant_id=tenant_a.id, activity_id=act.id, person_id=person.id, answers={})
    admin_session.add(sub)
    admin_session.flush()
    admin_session.add(Score(tenant_id=tenant_a.id, submission_id=sub.id, score=9, max_score=10,
                            fraction=0.9, passed=True, per_item=[], source="auto"))
    admin_session.commit()
    events = reminders._detect_events(admin_session, tenant_id=tenant_a.id, person_id=person.id,
                                      now=datetime.now(UTC), inactivity_days=7)
    assert not any(e["kind"] == "graded" for e in events)
