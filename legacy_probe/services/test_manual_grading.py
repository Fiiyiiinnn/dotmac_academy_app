"""Manual grading queue (Slice 4b, finding #4)."""

from __future__ import annotations

from app.models.assessment import Activity, Question, QuestionBank, Score
from app.models.course import Course
from app.services.assessment import override_score, pending_grading, submit_activity
from legacy_probe.conftest import make_person_party


def _manual_activity(db, tid):
    p = make_person_party(db, tenant_id=tid, email="m@a.edu", first_name="M", last_name="X")
    c = Course(tenant_id=tid, slug="m", title="M", discipline="networking", source_ref="x", version=1, status="published", listed=False)
    db.add_all([p, c])
    db.flush()
    bank = QuestionBank(tenant_id=tid, course_id=c.id, chapter_number=1, kind="chapter", version=1)
    db.add(bank)
    db.flush()
    db.add(Question(tenant_id=tid, bank_id=bank.id, ext_id="q1", stem="Explain", type="single",
                    options=["A", "B"], correct=["A"], rubric_category="analysis", explanation="", weight=1))
    act = Activity(tenant_id=tid, course_id=c.id, chapter_number=1, type="mcq_test", bank_id=bank.id,
                   title="Essay", pass_threshold=0.6, grading="manual", weight=1.0)
    db.add(act)
    db.flush()
    return p, c, act

def test_manual_submit_queues_then_grades(admin_session, tenant_a):
    tid = tenant_a.id
    p, c, act = _manual_activity(admin_session, tid)

    # Manual activity: submit creates a submission but no auto Score.
    result = submit_activity(admin_session, tenant_id=tid, person_id=p.id, activity=act,
                             answers={"q1": ["A"]})
    assert result is None
    queue = pending_grading(admin_session, tenant_id=tid)
    assert len(queue) == 1
    sub, activity, email = queue[0]
    assert activity.id == act.id and email == "m@a.edu"
    assert admin_session.query(Score).filter(Score.tenant_id == tid).count() == 0

    # Instructor grades it → leaves the queue.
    override_score(admin_session, tenant_id=tid, submission_id=sub.id, score_value=8,
                   max_score=10, reason="good")
    assert pending_grading(admin_session, tenant_id=tid) == []
    admin_session.rollback()
