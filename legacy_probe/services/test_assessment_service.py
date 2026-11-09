# tests/services/test_assessment_service.py
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.models.assessment import Activity, Question, QuestionBank
from app.models.course import Course
from app.models.email_outbox import EmailOutbox
from app.services.assessment import best_scores_for, override_score, submit_activity
from legacy_probe.conftest import make_person_party


def _seed(db, tid):
    c = Course(tenant_id=tid, slug="foundation", title="F", discipline="networking", source_ref="x", version=1, status="published", listed=False)
    db.add(c); db.flush()
    bank = QuestionBank(tenant_id=tid, course_id=c.id, chapter_number=3, kind="chapter", version=1)
    db.add(bank); db.flush()
    db.add(Question(tenant_id=tid, bank_id=bank.id, ext_id="q1", stem="?", type="single",
                    options=["A", "B"], correct=["A"], rubric_category="recall", explanation="", weight=1))
    act = Activity(tenant_id=tid, course_id=c.id, chapter_number=3, type="mcq_test",
                   bank_id=bank.id, title="Ch3", pass_threshold=0.6, grading="auto", weight=1.0)
    db.add(act); db.flush()
    return c, act

def test_submit_and_best(admin_session, tenant_a):
    # Best-of: a weak first attempt is superseded as the score of record by a
    # better retake, and a passing retake unlocks a learner who failed first.
    # A real person is needed — the passing retake fires a completion notification
    # whose FK references people.
    p = make_person_party(admin_session, tenant_id=tenant_a.id, email="sab@stu.edu", first_name="Sa", last_name="B")
    admin_session.add(p)
    admin_session.flush()
    c, act = _seed(admin_session, tenant_a.id)
    s1 = submit_activity(admin_session, tenant_id=tenant_a.id, person_id=p.id, activity=act, answers={"q1": ["B"]})
    s2 = submit_activity(admin_session, tenant_id=tenant_a.id, person_id=p.id, activity=act, answers={"q1": ["A"]})
    admin_session.flush()
    # Each attempt is scored; the passing retake is a distinct, better score.
    assert s1.passed is False and s2.passed is True and s2 is not s1
    best = best_scores_for(admin_session, tenant_id=tenant_a.id, person_id=p.id, course_id=c.id)
    assert best[act.id].fraction == 1.0
    admin_session.rollback()

def test_submit_activity_auto_on_pass_notifies_once(admin_session, tenant_a):
    """First passing submit queues one durable email; a later pass does not duplicate it."""
    c, act = _seed(admin_session, tenant_a.id)
    p = make_person_party(admin_session, tenant_id=tenant_a.id, email="pass@stu.edu", first_name="Pa", last_name="Ss")
    admin_session.add(p)
    admin_session.flush()

    def queued() -> int:
        return int(
            admin_session.scalar(
                select(func.count())
                .select_from(EmailOutbox)
                .where(EmailOutbox.tenant_id == tenant_a.id)
                .where(EmailOutbox.kind == "score_pass")
            )
            or 0
        )

    # Fail first → no delivery intent (each attempt is now scored).
    submit_activity(admin_session, tenant_id=tenant_a.id, person_id=p.id, activity=act, answers={"q1": ["B"]})
    assert queued() == 0
    # First pass → one transactional delivery intent.
    submit_activity(admin_session, tenant_id=tenant_a.id, person_id=p.id, activity=act, answers={"q1": ["A"]})
    assert queued() == 1
    # Second pass → still just one (deduped on prior passing scores).
    submit_activity(admin_session, tenant_id=tenant_a.id, person_id=p.id, activity=act, answers={"q1": ["A"]})
    assert queued() == 1
    admin_session.rollback()

def test_best_is_highest_not_latest(admin_session, tenant_a):
    """best_scores_for returns the highest attempt, not the most recent — pass is sticky."""
    # A real person row is required: a passing attempt fires a course-completion
    # notification whose FK references people.
    p = make_person_party(admin_session, tenant_id=tenant_a.id, email="best@stu.edu", first_name="Be", last_name="St")
    admin_session.add(p)
    admin_session.flush()
    c, act = _seed(admin_session, tenant_a.id)
    # First attempt: correct answer → fraction 1.0
    submit_activity(admin_session, tenant_id=tenant_a.id, person_id=p.id, activity=act, answers={"q1": ["A"]})
    # Second attempt: wrong answer → fraction 0.0 (latest, but lower — must not win)
    submit_activity(admin_session, tenant_id=tenant_a.id, person_id=p.id, activity=act, answers={"q1": ["B"]})
    admin_session.flush()
    best = best_scores_for(admin_session, tenant_id=tenant_a.id, person_id=p.id, course_id=c.id)
    assert best[act.id].fraction == 1.0, "best_scores_for returned latest (0.0), not highest (1.0)"
    admin_session.rollback()

def test_override_score_threshold_and_tenant_validation(admin_session, tenant_a):
    """override_score computes passed vs real threshold and rejects foreign submission_ids."""
    person = make_person_party(admin_session, tenant_id=tenant_a.id, email="override@stu.edu", first_name="Over", last_name="Ride")
    admin_session.add(person)
    admin_session.flush()
    person_id = person.id
    c, act = _seed(admin_session, tenant_a.id)  # act has pass_threshold=0.6
    # Create a real submission via submit_activity
    submit_activity(admin_session, tenant_id=tenant_a.id, person_id=person_id,
                    activity=act, answers={"q1": ["B"]})
    # Fetch the submission to get its id
    from sqlalchemy import select as sa_select

    from app.models.assessment import Submission as Sub
    sub = admin_session.scalars(
        sa_select(Sub).where(Sub.tenant_id == tenant_a.id).where(Sub.activity_id == act.id)
    ).first()
    # score_value=3, max_score=10 → frac=0.3 < 0.6 threshold → passed must be False
    result = override_score(admin_session, tenant_id=tenant_a.id, submission_id=sub.id,
                            score_value=3, max_score=10, reason="manual review")
    assert result.passed is False, f"expected passed=False, got {result.passed}"
    assert result.source == "override"
    # Tenant validation: random uuid should raise ValueError
    with pytest.raises(ValueError):
        override_score(admin_session, tenant_id=tenant_a.id, submission_id=uuid4(),
                       score_value=5, max_score=10, reason="should fail")
    admin_session.rollback()

def test_lab_activity_cannot_be_graded_as_a_quiz(admin_session, tenant_a):
    """Regression: a lab has no question bank, so grading it here produced a
    permanent 0/0 failed attempt with an empty per_item — no feedback, and it
    counted against course completion. Labs are graded by lab_lifecycle.grade."""
    from app.models.assessment import Submission
    from app.services.exceptions import BadRequestError

    p = make_person_party(admin_session, tenant_id=tenant_a.id, email="lab_guard@stu.edu", first_name="La", last_name="B")
    admin_session.add(p)
    c, _ = _seed(admin_session, tenant_a.id)
    lab = Activity(tenant_id=tenant_a.id, course_id=c.id, chapter_number=14, type="lab",
                   bank_id=None, title="Subnetting lab", pass_threshold=0.7, grading="auto", weight=1.0)
    admin_session.add(lab)
    admin_session.flush()

    with pytest.raises(BadRequestError):
        submit_activity(admin_session, tenant_id=tenant_a.id, person_id=p.id,
                        activity=lab, answers={})

    # No attempt was burned and no bogus Score recorded.
    attempts = admin_session.scalar(
        select(func.count()).select_from(Submission)
        .where(Submission.tenant_id == tenant_a.id).where(Submission.activity_id == lab.id)
    )
    admin_session.rollback()
    assert attempts == 0
