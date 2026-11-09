"""Success Queue Academy facade: CSV reports and the configured-policy hand-off.

The rules, sweep, segments and message commands are owned by dotmac-learning and covered by
Starter tests/test_learning_success_queue.py at the locked wheel; the web transitions/messages are
covered by Academy tests/services/test_genuine_success_queue_context.py. The audited acknowledge/resolve transition stays asserted here. See docs/inventories/test_dispositions.toml.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from dotmac_kernel.models import Party, PartyPerson, PartyType
from dotmac_kernel.session_runtime import resolve_database_runtime
from sqlalchemy import select

from app.models.assessment import Activity
from app.models.cohort import Cohort, Enrollment
from app.models.course import Course
from app.models.identity_extensions import AcademyAccountProfile
from app.models.offering import CourseOffering
from app.models.pacing import OfferingActivity
from app.models.success_queue import REASON_INACTIVITY, STATUS_RESOLVED, SuccessQueueEntry
from app.services import csv_reports, success_queue
from app.services.audit import list_events
from legacy_probe.services.roster_request_fixtures import roster_actors

assert roster_actors  # fixture import


def _seed(admin_session, tenant, *, email, due_hours=None, enrolled_days_ago=30):
    person = Party(
        tenant_id=tenant.id, email=email, display_name="Sq Learner", party_type=PartyType.person, is_active=True
    )
    admin_session.add(person)
    admin_session.flush()
    admin_session.add(PartyPerson(party_id=person.id, first_name="Sq", last_name="Learner"))
    admin_session.flush()
    admin_session.add(
        AcademyAccountProfile(
            tenant_id=tenant.id,
            party_id=person.id,
            status="active",
            prefs={},
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    cohort = Cohort(tenant_id=tenant.id, name=f"SQ Cohort {email}", discipline="fiber", status="active")
    admin_session.add(cohort)
    course = Course(
        tenant_id=tenant.id,
        slug=f"sq-{email.split('@')[0]}",
        title="SQ Course",
        discipline="fiber",
        source_ref="t@1",
        status="published",
        listed=False,
    )
    admin_session.add(course)
    admin_session.flush()
    enr = Enrollment(
        tenant_id=tenant.id, cohort_id=cohort.id, person_id=person.id, role_in_cohort="student", status="active"
    )
    admin_session.add(enr)
    admin_session.flush()
    enr.created_at = datetime.now(UTC) - timedelta(days=enrolled_days_ago)
    off = CourseOffering(tenant_id=tenant.id, cohort_id=cohort.id, course_id=course.id, status="active")
    admin_session.add(off)
    admin_session.flush()
    act = None
    if due_hours is not None:
        act = Activity(
            tenant_id=tenant.id,
            course_id=course.id,
            chapter_number=1,
            type="mcq_test",
            title="SQ act",
            pass_threshold=0.6,
            assessment_mode="graded",
            grading="auto",
            weight=1.0,
        )
        admin_session.add(act)
        admin_session.flush()
        admin_session.add(
            OfferingActivity(
                tenant_id=tenant.id,
                offering_id=off.id,
                activity_id=act.id,
                due_at=datetime.now(UTC) + timedelta(hours=due_hours),
            )
        )
    admin_session.commit()
    return person, cohort, course, act


def _entries(db, tenant, person, reason=None):
    # Shared with tests/services/test_genuine_success_queue_context.py.
    q = (
        select(SuccessQueueEntry)
        .where(SuccessQueueEntry.tenant_id == tenant.id)
        .where(SuccessQueueEntry.person_id == person.id)
    )
    if reason:
        q = q.where(SuccessQueueEntry.reason_kind == reason)
    return db.scalars(q).all()


def test_transitions_are_audited(roster_actors):
    a = roster_actors.account
    admin_session, tenant_a, actor = a.admin, a.tenant, roster_actors.instructor
    person, *_ = _seed(admin_session, tenant_a, email="trans@a.edu")
    success_queue.sweep(admin_session, tenant_id=tenant_a.id)
    admin_session.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    admin_session = a.db
    (entry,) = _entries(admin_session, tenant_a, person, REASON_INACTIVITY)

    success_queue.transition(
        admin_session, tenant_id=tenant_a.id, entry_id=entry.id, action="acknowledge", request=actor.request
    )
    success_queue.transition(
        admin_session, tenant_id=tenant_a.id, entry_id=entry.id, action="resolve", request=actor.request
    )
    admin_session.commit()
    resolve_database_runtime().set_tenant(admin_session, a.tid)
    assert entry.status == STATUS_RESOLVED
    assert entry.resolved_by == actor.pid
    actions = [e.action for e in list_events(admin_session, tenant_id=tenant_a.id, limit=20)]
    assert "success_queue.acknowledge" in actions
    assert "success_queue.resolve" in actions


def test_csv_reports_content(admin_session, tenant_a):
    person, cohort, *_ = _seed(admin_session, tenant_a, email="csv@a.edu", enrolled_days_ago=15)
    success_queue.sweep(admin_session, tenant_id=tenant_a.id)
    admin_session.commit()

    name, text = csv_reports.cohort_roster_csv(admin_session, tenant_id=tenant_a.id, cohort_id=cohort.id)
    assert name.startswith("roster-progress-") and name.endswith(".csv")
    lines = text.strip().split("\r\n")
    assert lines[0].startswith("name,email,courses")
    assert any(person.email in line and "inactivity" in line for line in lines[1:])

    qname, qtext = csv_reports.queue_summary_csv(admin_session, tenant_id=tenant_a.id, cohort_id=cohort.id)
    qlines = qtext.strip().split("\r\n")
    assert qlines[0].startswith("name,email,cohort,reason")
    assert any("inactivity" in line for line in qlines[1:])

    fname, ftext = csv_reports.cohort_funnel_csv(admin_session, tenant_id=tenant_a.id, cohort_id=cohort.id)
    assert "enrolled,1" in ftext.replace("\r", "")


def test_adapter_forwards_configured_success_policy_to_public_owner(monkeypatch):
    import dotmac_learning as learning

    session = object()
    tenant = uuid4()
    terms = learning.SuccessTerms(
        3, 61, 2, 3, 4, 5, 1, 3, 0.4, 0.6, 0.9,
        ("inactivity", "overdue_work", "below_passing", "failed_final", "certificate_blocked"),
        {"failed_final": "high", "certificate_blocked": "medium"},
    )
    calls = []
    monkeypatch.setattr(success_queue, "success_terms", lambda db: terms)

    def owner(db, **kw):
        assert db is session and kw["terms"] is terms
        assert kw["accounts"].tenant_id == tenant
        calls.append(kw)
        return learning.SweepResult(1, 2, 3)

    monkeypatch.setattr(success_queue.learning, "sweep_success_queue", owner)
    assert success_queue.sweep(session, tenant_id=tenant) == {"opened": 1, "refreshed": 2, "auto_resolved": 3}
    assert len(calls) == 1 and calls[0]["now"] is None
