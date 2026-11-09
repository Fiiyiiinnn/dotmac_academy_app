"""Item analysis (Slice 4c, finding #4)."""

from __future__ import annotations

from app.models.assessment import Activity, Score, Submission
from app.models.course import Course
from app.services.analytics import item_analysis
from legacy_probe.conftest import make_person_party


def _score(db, tid, activity_id, person_id, per_item, fraction, *, attempt_no=1):
    sub = Submission(tenant_id=tid, activity_id=activity_id, person_id=person_id,
                     answers={}, attempt_no=attempt_no)
    db.add(sub)
    db.flush()
    db.add(Score(tenant_id=tid, submission_id=sub.id, score=fraction * 10, max_score=10,
                 fraction=fraction, passed=fraction >= 0.6, per_item=per_item, source="auto"))
    db.flush()

def test_item_analysis_p_values(admin_session, tenant_a):
    tid = tenant_a.id
    c = Course(tenant_id=tid, slug="a", title="A", discipline="networking", source_ref="x", version=1, status="published", listed=False)
    admin_session.add(c)
    admin_session.flush()
    act = Activity(tenant_id=tid, course_id=c.id, chapter_number=1, type="mcq_test",
                   title="Quiz", pass_threshold=0.6, grading="auto", weight=1.0)
    admin_session.add(act)
    admin_session.flush()
    pa = make_person_party(admin_session, tenant_id=tid, email="pa@a.edu", first_name="P", last_name="A")
    pb = make_person_party(admin_session, tenant_id=tid, email="pb@a.edu", first_name="P", last_name="B")
    admin_session.add_all([pa, pb])
    admin_session.flush()

    # q1: both correct (p=1.0). q2: one correct (p=0.5).
    _score(admin_session, tid, act.id, pa.id,
           [{"id": "q1", "correct": True}, {"id": "q2", "correct": True}], 1.0)
    _score(admin_session, tid, act.id, pb.id,
           [{"id": "q1", "correct": True}, {"id": "q2", "correct": False}], 0.5)

    items = {i["id"]: i for i in item_analysis(admin_session, tenant_id=tid, activity_id=act.id)}
    assert items["q1"]["responses"] == 2 and items["q1"]["correct"] == 2
    assert items["q1"]["p_value"] == 1.0
    assert items["q2"]["responses"] == 2 and items["q2"]["correct"] == 1
    assert items["q2"]["p_value"] == 0.5
    admin_session.rollback()

def test_item_analysis_uses_best_attempt(admin_session, tenant_a):
    """A worse later attempt must not lower the item stats."""
    tid = tenant_a.id
    c = Course(tenant_id=tid, slug="b", title="B", discipline="networking", source_ref="x", version=1, status="published", listed=False)
    admin_session.add(c)
    admin_session.flush()
    act = Activity(tenant_id=tid, course_id=c.id, chapter_number=1, type="mcq_test",
                   title="Quiz", pass_threshold=0.6, grading="auto", weight=1.0)
    admin_session.add(act)
    admin_session.flush()
    p = make_person_party(admin_session, tenant_id=tid, email="p@a.edu", first_name="P", last_name="X")
    admin_session.add(p)
    admin_session.flush()

    _score(admin_session, tid, act.id, p.id, [{"id": "q1", "correct": True}], 1.0)   # best
    _score(
        admin_session,
        tid,
        act.id,
        p.id,
        [{"id": "q1", "correct": False}],
        0.0,
        attempt_no=2,
    )  # worse later

    items = {i["id"]: i for i in item_analysis(admin_session, tenant_id=tid, activity_id=act.id)}
    assert items["q1"]["responses"] == 1
    assert items["q1"]["p_value"] == 1.0
    admin_session.rollback()
