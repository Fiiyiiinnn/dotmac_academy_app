"""Regression tests for whole-branch review fixes (feat/lms-gaps).

- numeric question with malformed author tolerance must not raise at grade time
- announcement creation must survive a notification fan-out failure (savepoint)
"""

from __future__ import annotations

from app.models.announcement import Announcement
from app.models.cohort import Cohort, Enrollment
from app.services import announcements as ann_mod
from app.services.grading import grade_submission
from legacy_probe.conftest import make_person_party


def test_numeric_malformed_tolerance_does_not_raise():
    q = {
        "ext_id": "q1",
        "type": "numeric",
        "correct": [10],
        "options": {"tolerance": "not-a-number"},
        "weight": 1,
    }
    # Falls back to tolerance 0: exact answer passes, off-by-one fails — no exception.
    assert grade_submission({"q1": ["10"]}, [q], 0.5, missing_question_weight=1).per_item[0]["correct"] is True
    assert grade_submission({"q1": ["11"]}, [q], 0.5, missing_question_weight=1).per_item[0]["correct"] is False

def test_announcement_survives_notify_failure(admin_session, tenant_a, monkeypatch):
    tid = tenant_a.id
    author = make_person_party(admin_session, tenant_id=tid, email="author@a.edu", first_name="A", last_name="U")
    coh = Cohort(tenant_id=tid, name="C", discipline="networking", status="active")
    admin_session.add_all([author, coh])
    admin_session.flush()
    stu = make_person_party(admin_session, tenant_id=tid, email="stu@a.edu", first_name="S", last_name="T")
    admin_session.add(stu)
    admin_session.flush()
    admin_session.add(
        Enrollment(tenant_id=tid, cohort_id=coh.id, person_id=stu.id,
                   role_in_cohort="student", status="active")
    )
    admin_session.flush()

    def boom(*a, **k):
        raise RuntimeError("notify backend down")

    monkeypatch.setattr(ann_mod, "notify_many", boom)

    ann = ann_mod.create(
        admin_session, tenant_id=tid, author_person_id=author.id,
        title="Heads up", body_md="body",
    )
    admin_session.flush()
    # The announcement persists despite the fan-out failure (savepoint isolated it).
    assert (
        admin_session.query(Announcement).filter(Announcement.id == ann.id).count() == 1
    )
    admin_session.rollback()
