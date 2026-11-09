"""Learning-event ledger + insights (roadmap P3a).

Pins the append-only writer's dedupe rules, the savepoint isolation of
``emit``, the emission wiring of the assessment/attempt owners, and the
bucket/active/funnel math the insight projections rely on.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from dotmac_kernel.models import Party

from app.models.course import Course
from app.models.learning_event import (
    KIND_ACTIVITY_STARTED,
    KIND_CHAPTER_COMPLETED,
    KIND_COURSE_VIEWED,
    KIND_SUBMISSION_MADE,
    KIND_WORK_GRADED,
    LearningEvent,
)
from app.services import insights, learning_events
from legacy_probe.conftest import make_person_party

pytestmark = pytest.mark.usefixtures("tenant_a")

def _person(db, tid, email="ledger@a.edu"):
    p = make_person_party(db, tenant_id=tid, email=email, first_name="Led", last_name="Ger")
    db.add(p)
    db.flush()
    return p

def _course(db, tid, slug="ledger-course"):
    c = Course(
        tenant_id=tid,
        slug=slug,
        title="Ledger Course",
        discipline="networking",
        source_ref="t@1",
        status="published",
        listed=False,
    )
    db.add(c)
    db.flush()
    return c

def _auto_activity(db, tid, email="auto@a.edu", slug="ledger-auto"):
    """Auto-graded activity + person + a correct answer set."""
    from app.models.assessment import Activity, Question, QuestionBank

    person = _person(db, tid, email=email)
    course = _course(db, tid, slug=slug)
    bank = QuestionBank(tenant_id=tid, course_id=course.id, chapter_number=1, kind="chapter", version=1)
    db.add(bank)
    db.flush()
    db.add(
        Question(
            tenant_id=tid,
            bank_id=bank.id,
            ext_id="q1",
            stem="Pick A",
            type="single",
            options=["A", "B"],
            correct=["A"],
            rubric_category="recall",
            explanation="",
            weight=1,
        )
    )
    activity = Activity(
        tenant_id=tid,
        course_id=course.id,
        chapter_number=1,
        type="mcq_test",
        bank_id=bank.id,
        title="Auto",
        pass_threshold=0.5,
        assessment_mode="graded",
        grading="auto",
        weight=1.0,
    )
    db.add(activity)
    db.flush()
    return activity, person, {"q1": ["A"]}

def test_once_per_subject_and_daily_dedupe(admin_session, tenant_a):
    tid = tenant_a.id
    p = _person(admin_session, tid)
    c = _course(admin_session, tid)
    chapter_id = uuid4()

    # chapter_completed: once per subject, ever.
    e1 = learning_events.record(
        admin_session, tenant_id=tid, person_id=p.id, kind=KIND_CHAPTER_COMPLETED, course_id=c.id, subject_id=chapter_id
    )
    e2 = learning_events.record(
        admin_session, tenant_id=tid, person_id=p.id, kind=KIND_CHAPTER_COMPLETED, course_id=c.id, subject_id=chapter_id
    )
    assert e1 is not None and e2 is None

    # course_viewed: once per local day, next day allowed.
    now = datetime.now(UTC)
    v1 = learning_events.record(
        admin_session,
        tenant_id=tid,
        person_id=p.id,
        kind=KIND_COURSE_VIEWED,
        course_id=c.id,
        subject_id=c.id,
        occurred_at=now,
    )
    v2 = learning_events.record(
        admin_session,
        tenant_id=tid,
        person_id=p.id,
        kind=KIND_COURSE_VIEWED,
        course_id=c.id,
        subject_id=c.id,
        occurred_at=now,
    )
    v3 = learning_events.record(
        admin_session,
        tenant_id=tid,
        person_id=p.id,
        kind=KIND_COURSE_VIEWED,
        course_id=c.id,
        subject_id=c.id,
        occurred_at=now + timedelta(days=1),
    )
    assert v1 is not None and v2 is None and v3 is not None
    admin_session.commit()

def test_emit_never_breaks_host_flow(admin_session, tenant_a):
    p = _person(admin_session, tenant_a.id, email="emit@a.edu")
    # Unknown kind raises inside record; emit must swallow it and keep the
    # session usable for further writes in the same transaction.
    learning_events.emit(admin_session, tenant_id=tenant_a.id, person_id=p.id, kind="not_a_real_kind")
    p.first_name = "Still"
    admin_session.flush()  # would raise if the transaction were poisoned
    assert admin_session.get(Party, p.id).first_name == "Still"
    admin_session.commit()

def test_record_rejects_unknown_kind(admin_session, tenant_a):
    p = _person(admin_session, tenant_a.id, email="kind@a.edu")
    with pytest.raises(ValueError):
        learning_events.record(admin_session, tenant_id=tenant_a.id, person_id=p.id, kind="bogus")
    admin_session.rollback()

def test_submit_activity_emits_submission_and_grade(admin_session, tenant_a):
    """The assessment owner's commit point writes both observation kinds."""
    from app.services.assessment import submit_activity

    activity, person, answers = _auto_activity(admin_session, tenant_a.id, email="sub@a.edu", slug="ledger-sub")
    submit_activity(admin_session, tenant_id=tenant_a.id, person_id=person.id, activity=activity, answers=answers)
    kinds = {e.kind for e in admin_session.query(LearningEvent).filter(LearningEvent.person_id == person.id).all()}
    assert KIND_SUBMISSION_MADE in kinds and KIND_WORK_GRADED in kinds
    admin_session.commit()

def test_open_attempt_emits_started_once_per_attempt(admin_session, tenant_a):
    from app.services.attempts import open_or_create_attempt

    activity, person, _ = _auto_activity(admin_session, tenant_a.id, email="att@a.edu", slug="ledger-att")
    open_or_create_attempt(
        admin_session, tenant_id=tenant_a.id, person_id=person.id, activity_id=activity.id, all_ext_ids=["q1"], count=1
    )
    # Second call returns the same open attempt — no second event.
    open_or_create_attempt(
        admin_session, tenant_id=tenant_a.id, person_id=person.id, activity_id=activity.id, all_ext_ids=["q1"], count=1
    )
    count = (
        admin_session.query(LearningEvent)
        .filter(LearningEvent.person_id == person.id)
        .filter(LearningEvent.kind == KIND_ACTIVITY_STARTED)
        .count()
    )
    assert count == 1
    admin_session.commit()

def test_weekly_buckets_math(admin_session, tenant_a):
    tid = tenant_a.id
    p = _person(admin_session, tid, email="weeks@a.edu")
    # Anchored at midday, not "right now". weekly_buckets compares .date()
    # boundaries while the events are placed with hour offsets, so a run in the
    # small hours pushed events back over midnight and out of their bucket —
    # this test failed at 01:53 UTC and passed at every other hour.
    now = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    for weeks_ago, n in [(1, 3), (2, 1)]:
        for i in range(n):
            learning_events.record(
                admin_session,
                tenant_id=tid,
                person_id=p.id,
                kind=KIND_ACTIVITY_STARTED,
                subject_id=uuid4(),
                occurred_at=now - timedelta(weeks=weeks_ago, hours=i + 1),
            )
    buckets = learning_events.weekly_buckets(admin_session, tenant_id=tid, person_id=p.id, weeks=4, now=now)
    assert len(buckets) == 4
    assert buckets[-1]["count"] == 3  # 0-1 weeks ago
    assert buckets[-2]["count"] == 1  # 1-2 weeks ago
    admin_session.commit()

def test_active_person_ids_window(admin_session, tenant_a):
    tid = tenant_a.id
    active = _person(admin_session, tid, email="active@a.edu")
    idle = _person(admin_session, tid, email="idle@a.edu")
    now = datetime.now(UTC)
    learning_events.record(
        admin_session,
        tenant_id=tid,
        person_id=active.id,
        kind=KIND_ACTIVITY_STARTED,
        subject_id=uuid4(),
        occurred_at=now - timedelta(days=2),
    )
    learning_events.record(
        admin_session,
        tenant_id=tid,
        person_id=idle.id,
        kind=KIND_ACTIVITY_STARTED,
        subject_id=uuid4(),
        occurred_at=now - timedelta(days=30),
    )
    got = learning_events.active_person_ids(
        admin_session,
        tenant_id=tid,
        person_ids=[active.id, idle.id],
        since=now - timedelta(days=7),
    )
    assert got == {active.id}
    admin_session.commit()

def test_learner_activity_projection_shape(admin_session, tenant_a):
    tid = tenant_a.id
    p = _person(admin_session, tid, email="proj@a.edu")
    out = insights.learner_activity(admin_session, tenant_id=tid, person_id=p.id)
    assert out["days_since_last"] is None
    assert out["blocker"] is None
    assert "Start" in out["recommendation"]
    assert len(out["weekly"]) == 8
    admin_session.commit()

def test_weekly_bucket_adapter_resolves_omitted_window_and_preserves_explicit_zero(monkeypatch):
    from types import SimpleNamespace

    calls = []
    session = object()
    monkeypatch.setattr(learning_events, "learner_insight_terms", lambda db: SimpleNamespace(chart_weeks=3))
    monkeypatch.setattr(learning_events.learning, "weekly_buckets", lambda db, **kw: calls.append((db, kw)) or [])
    tenant, person = uuid4(), uuid4()
    assert learning_events.weekly_buckets(session, tenant_id=tenant, person_id=person) == []
    assert learning_events.weekly_buckets(session, tenant_id=tenant, person_id=person, weeks=0) == []
    assert [kw["weeks"] for db, kw in calls] == [3, 0]
    assert all(db is session for db, kw in calls)
