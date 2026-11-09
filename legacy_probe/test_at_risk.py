"""At-risk detection + learner nudges."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.services import at_risk
from legacy_probe.conftest import make_person_party


def _now():
    return datetime.now(UTC)

def _setup(admin_session, tenant, *, starts, ends, pct, status="in_progress"):
    """A student enrolled in a cohort running one course, with a completion pct."""
    from app.models.cohort import Cohort, Enrollment
    from app.models.completion import CourseCompletion
    from app.models.course import Course
    from app.models.offering import CourseOffering

    admin_session.rollback()
    import uuid

    suffix = uuid.uuid4().hex[:6]
    cohort = Cohort(tenant_id=tenant.id, name=f"C-{suffix}", discipline="fiber", status="active")
    course = Course(
        tenant_id=tenant.id, title=f"Fiber {suffix}", slug=f"fiber-{suffix}", discipline="fiber", source_ref="x",
        status="published", listed=False,
    )
    person = make_person_party(admin_session, tenant_id=tenant.id, email=f"s{suffix}@a.ex", first_name="S", last_name="T")
    admin_session.add_all([cohort, course, person])
    admin_session.flush()
    admin_session.add_all(
        [
            Enrollment(
                tenant_id=tenant.id, cohort_id=cohort.id, person_id=person.id, role_in_cohort="student", status="active"
            ),
            CourseOffering(
                tenant_id=tenant.id,
                cohort_id=cohort.id,
                course_id=course.id,
                status="active",
                starts_at=starts,
                ends_at=ends,
            ),
            CourseCompletion(tenant_id=tenant.id, person_id=person.id, course_id=course.id, status=status, pct=pct),
        ]
    )
    admin_session.commit()
    return person, course

def test_overdue_flagged(admin_session, tenant_a):
    p, _ = _setup(admin_session, tenant_a, starts=_now() - timedelta(days=30), ends=_now() - timedelta(days=1), pct=0.4)
    items = at_risk.at_risk_for_person(admin_session, tenant_id=tenant_a.id, person_id=p.id)
    assert len(items) == 1 and items[0]["reason"] == "overdue"

def test_behind_flagged(admin_session, tenant_a):
    # window 80% elapsed, only 10% done -> behind
    p, _ = _setup(admin_session, tenant_a, starts=_now() - timedelta(days=8), ends=_now() + timedelta(days=2), pct=0.1)
    items = at_risk.at_risk_for_person(admin_session, tenant_id=tenant_a.id, person_id=p.id)
    assert len(items) == 1 and items[0]["reason"] == "behind"

def test_on_track_not_flagged(admin_session, tenant_a):
    # window 50% elapsed, 60% done -> ahead of pace
    p, _ = _setup(admin_session, tenant_a, starts=_now() - timedelta(days=5), ends=_now() + timedelta(days=5), pct=0.6)
    assert at_risk.at_risk_for_person(admin_session, tenant_id=tenant_a.id, person_id=p.id) == []

def test_completed_not_flagged(admin_session, tenant_a):
    p, _ = _setup(
        admin_session,
        tenant_a,
        starts=_now() - timedelta(days=30),
        ends=_now() - timedelta(days=1),
        pct=1.0,
        status="completed",
    )
    assert at_risk.at_risk_for_person(admin_session, tenant_id=tenant_a.id, person_id=p.id) == []

def test_notify_and_dedup(admin_session, tenant_a):
    from app.models.notification import Notification

    p, _ = _setup(admin_session, tenant_a, starts=_now() - timedelta(days=30), ends=_now() - timedelta(days=1), pct=0.2)
    n1 = at_risk.notify_person_if_at_risk(admin_session, tenant_id=tenant_a.id, person_id=p.id)
    admin_session.commit()
    assert n1 == 1
    # re-run: the unread nudge already exists -> no duplicate
    n2 = at_risk.notify_person_if_at_risk(admin_session, tenant_id=tenant_a.id, person_id=p.id)
    admin_session.commit()
    assert n2 == 0
    total = admin_session.scalar(
        select(func.count())
        .select_from(Notification)
        .where(Notification.person_id == p.id)
        .where(Notification.kind == "at_risk")
    )
    assert total == 1
