"""Certificate auto-issue on completion for a learner who opted out of result emails.

The grading/rule parts that used to live here (attempted_grade, the below-passing and
certificate-blocked success-queue rules) are owned by dotmac-learning and covered by Starter
tests/test_learning_success_queue.py and tests/test_learning_gradebook_reports.py at the locked wheel;
see docs/inventories/test_dispositions.toml.
"""

from __future__ import annotations

from app.models.assessment import Activity, Score, Submission
from app.models.certificate import Certificate
from app.models.cohort import Cohort, Enrollment
from app.models.course import Course
from app.models.email_outbox import EmailOutbox
from app.models.offering import CourseOffering
from app.services.certificates import issue_certificate
from app.services.completion import recompute_completion
from legacy_probe.conftest import make_person_party


def _course_with_activities(admin_session, tenant, slug, *, n, prefs=None):
    person = make_person_party(
        admin_session, tenant_id=tenant.id, email=f"{slug}@a.edu", first_name="G", last_name="C", prefs=prefs
    )
    course = Course(tenant_id=tenant.id, slug=slug, title=slug.title(),
                    discipline="fiber", source_ref="t@1", status="published", listed=False)
    admin_session.add(course)
    cohort = Cohort(tenant_id=tenant.id, name=f"C {slug}", discipline="fiber", status="active")
    admin_session.add(cohort)
    admin_session.flush()
    admin_session.add(Enrollment(tenant_id=tenant.id, cohort_id=cohort.id, person_id=person.id,
                                 role_in_cohort="student", status="active"))
    admin_session.add(CourseOffering(tenant_id=tenant.id, cohort_id=cohort.id,
                                     course_id=course.id, status="active"))
    acts = []
    for i in range(n):
        a = Activity(tenant_id=tenant.id, course_id=course.id, chapter_number=i + 1,
                     type="mcq_test", title=f"A{i}", pass_threshold=0.6, weight=1, grading="auto")
        admin_session.add(a)
        acts.append(a)
    admin_session.flush()
    return person, course, acts


def _score(admin_session, tenant, person, act, fraction):
    sub = Submission(tenant_id=tenant.id, activity_id=act.id, person_id=person.id, answers={})
    admin_session.add(sub)
    admin_session.flush()
    admin_session.add(Score(tenant_id=tenant.id, submission_id=sub.id, score=int(fraction * 10),
                            max_score=10, fraction=fraction, passed=fraction >= 0.6,
                            per_item=[], source="auto"))
    admin_session.flush()


def test_certificate_auto_issued_idempotently_when_result_emails_are_opted_out(admin_session, tenant_a):
    # email_results=False lives on the AcademyAccountProfile prefs, not on the party.
    person, course, acts = _course_with_activities(
        admin_session, tenant_a, "cert-auto", n=2, prefs={"email_results": False}
    )
    for a in acts:
        _score(admin_session, tenant_a, person, a, 1.0)
    admin_session.flush()
    rec = recompute_completion(admin_session, tenant_id=tenant_a.id, person_id=person.id, course_id=course.id)
    admin_session.commit()
    assert rec.status == "completed"
    cert = admin_session.query(Certificate).filter_by(
        tenant_id=tenant_a.id, person_id=person.id, course_id=course.id).one()
    assert cert.serial and cert.issued_at is not None
    # Issuance happened but the opt-out suppressed the result email.
    assert admin_session.query(EmailOutbox).filter_by(tenant_id=tenant_a.id, recipient=person.email).count() == 0
    # Idempotent: a second issue returns the same record and still queues no email.
    again = issue_certificate(admin_session, tenant_id=tenant_a.id, person_id=person.id, course_id=course.id)
    assert again.id == cert.id
    admin_session.commit()
    assert admin_session.query(Certificate).filter_by(
        tenant_id=tenant_a.id, person_id=person.id, course_id=course.id).count() == 1
    assert admin_session.query(EmailOutbox).filter_by(tenant_id=tenant_a.id, recipient=person.email).count() == 0
