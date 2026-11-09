"""Reminder policy service (roadmap P2): idempotence, boundaries, pacing.

The sweep is the single decision owner; these tests pin its contract:
once-per-occurrence, event-kind boundaries, opt-outs, digest batching,
quiet-hours deferral, and the audited admin resend.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select

from app.models.assessment import Activity
from app.models.class_session import ClassSession
from app.models.cohort import Cohort, Enrollment
from app.models.course import Course
from app.models.email_outbox import EmailOutbox
from app.models.notification import Notification
from app.models.offering import CourseOffering
from app.models.pacing import OfferingActivity
from app.models.reminder import ReminderLog
from app.services import reminders
from app.services.audit import list_events
from app.services.localtime import academy_zone
from legacy_probe.conftest import make_person_party
from legacy_probe.services.test_canonical_current_user import _request, current_account
from legacy_probe.web.test_product_admin_routes_owner import _allow, _restore_policy

assert current_account and _restore_policy


def _seed(admin_session, tenant, *, email="rem@a.edu", due_offsets_hours=(), session_offsets_hours=()):
    person = make_person_party(admin_session, tenant_id=tenant.id, email=email, first_name="Re", last_name="Mind")
    admin_session.add(person)
    cohort = Cohort(tenant_id=tenant.id, name=f"Rem Cohort {email}", discipline="fiber", status="active")
    admin_session.add(cohort)
    course = Course(tenant_id=tenant.id, slug=f"rem-{email.split('@')[0]}", title="Reminder Course",
                    discipline="fiber", source_ref="t@1", status="published", listed=False)
    admin_session.add(course)
    admin_session.flush()
    admin_session.add(Enrollment(tenant_id=tenant.id, cohort_id=cohort.id, person_id=person.id,
                                 role_in_cohort="student", status="active"))
    off = CourseOffering(tenant_id=tenant.id, cohort_id=cohort.id, course_id=course.id, status="active")
    admin_session.add(off)
    admin_session.flush()
    now = datetime.now(UTC)
    for n, hours in enumerate(due_offsets_hours):
        act = Activity(tenant_id=tenant.id, course_id=course.id, chapter_number=n + 1,
                       type="mcq_test", title=f"Rem act {n}", pass_threshold=0.6, grading="auto", weight=1.0)
        admin_session.add(act)
        admin_session.flush()
        admin_session.add(OfferingActivity(tenant_id=tenant.id, offering_id=off.id,
                                           activity_id=act.id, due_at=now + timedelta(hours=hours)))
    for n, hours in enumerate(session_offsets_hours):
        admin_session.add(ClassSession(
            tenant_id=tenant.id, cohort_id=cohort.id, title=f"Rem session {n}",
            session_type="live_class", starts_at=now + timedelta(hours=hours),
            ends_at=now + timedelta(hours=hours + 1), join_url="https://meet.example/x",
        ))
    admin_session.commit()
    return person, cohort, course, off

def _log_rows(db, tenant, person):
    return db.scalars(
        select(ReminderLog)
        .where(ReminderLog.tenant_id == tenant.id)
        .where(ReminderLog.person_id == person.id)
    ).all()

def _outbox_rows(db, tenant):
    return db.scalars(select(EmailOutbox).where(EmailOutbox.tenant_id == tenant.id)).all()

def test_sweep_is_idempotent(admin_session, tenant_a):
    person, *_ = _seed(admin_session, tenant_a, email="idem@a.edu", due_offsets_hours=(20,))
    now = datetime.now(UTC)
    first = reminders.sweep(admin_session, tenant_id=tenant_a.id, now=now)
    reminders.sweep(admin_session, tenant_id=tenant_a.id, now=now + timedelta(minutes=5))
    admin_session.commit()
    keys = [r.occurrence_key for r in _log_rows(admin_session, tenant_a, person)]
    assert first["detected"] >= 1
    assert len(keys) == len(set(keys))
    assert len([k for k in keys if k.startswith("due_24h:")]) == 1

def test_event_boundaries_due_and_sessions(admin_session, tenant_a):
    person, *_ = _seed(admin_session, tenant_a, email="bound@a.edu",
                       due_offsets_hours=(-3, 20, 60), session_offsets_hours=(0.5, 20))
    reminders.sweep(admin_session, tenant_id=tenant_a.id)
    admin_session.commit()
    kinds = {r.event_kind for r in _log_rows(admin_session, tenant_a, person)}
    assert {"overdue", "due_24h", "due_72h", "session_1h", "session_24h",
            "course_assigned", "course_starting"} - kinds == {"course_starting"}
    # course_starting requires a future offering window; none set here.

def test_optout_suppresses_both_channels(admin_session, tenant_a):
    person, *_ = _seed(admin_session, tenant_a, email="optout@a.edu", due_offsets_hours=(20,))
    reminders.save_preference(admin_session, tenant_id=tenant_a.id, person_id=person.id,
                              frequency="immediate", quiet_start_hour=None, quiet_end_hour=None,
                              optouts=["due_24h", "course_assigned"])
    before_outbox = len(_outbox_rows(admin_session, tenant_a))
    reminders.sweep(admin_session, tenant_id=tenant_a.id)
    admin_session.commit()
    rows = _log_rows(admin_session, tenant_a, person)
    assert rows and all(r.status == "skipped" for r in rows)
    assert len(_outbox_rows(admin_session, tenant_a)) == before_outbox
    notes = admin_session.scalars(
        select(Notification).where(Notification.tenant_id == tenant_a.id)
        .where(Notification.person_id == person.id)).all()
    assert notes == []

def test_daily_digest_batches_into_one_email(admin_session, tenant_a):
    person, *_ = _seed(admin_session, tenant_a, email="digest@a.edu", due_offsets_hours=(20, 60))
    reminders.save_preference(admin_session, tenant_id=tenant_a.id, person_id=person.id,
                              frequency="daily_digest", quiet_start_hour=None, quiet_end_hour=None,
                              optouts=[])
    # Deterministic clocks: detection at 10:00 local (queues, no email),
    # flush at 07:00 local next day (the digest hour).
    local_now = datetime.now(UTC).astimezone(academy_zone())
    now_off = local_now.replace(hour=10, minute=0, second=0, microsecond=0).astimezone(UTC)
    before_outbox = len(_outbox_rows(admin_session, tenant_a))
    reminders.sweep(admin_session, tenant_id=tenant_a.id, now=now_off)
    admin_session.commit()
    queued = [r for r in _log_rows(admin_session, tenant_a, person) if r.status == "queued"]
    assert len(queued) >= 2
    assert len(_outbox_rows(admin_session, tenant_a)) == before_outbox
    digest_now = (local_now + timedelta(days=1)).replace(hour=7, minute=0, second=0,
                                                         microsecond=0).astimezone(UTC)
    reminders.sweep(admin_session, tenant_id=tenant_a.id, now=digest_now)
    admin_session.commit()
    rows = _log_rows(admin_session, tenant_a, person)
    assert all(r.status == "sent" for r in rows if r.channel == "email")
    digest_emails = [o for o in _outbox_rows(admin_session, tenant_a) if o.kind == "reminder_digest"
                     and o.recipient == "digest@a.edu"]
    assert len(digest_emails) == 1
    assert "Rem act 0" in digest_emails[0].html_body and "Rem act 1" in digest_emails[0].html_body

def test_quiet_hours_defer_then_flush(admin_session, tenant_a):
    person, *_ = _seed(admin_session, tenant_a, email="quiet@a.edu", due_offsets_hours=(20,))
    hour_now = datetime.now(UTC).astimezone(academy_zone()).hour
    reminders.save_preference(admin_session, tenant_id=tenant_a.id, person_id=person.id,
                              frequency="immediate",
                              quiet_start_hour=hour_now, quiet_end_hour=(hour_now + 2) % 24,
                              optouts=[])
    before = len(_outbox_rows(admin_session, tenant_a))
    reminders.sweep(admin_session, tenant_id=tenant_a.id)
    admin_session.commit()
    rows = _log_rows(admin_session, tenant_a, person)
    assert rows and all(r.status == "queued" for r in rows)
    assert len(_outbox_rows(admin_session, tenant_a)) == before
    # Outside quiet hours the deferred reminders flush as one catch-up email.
    later = datetime.now(UTC) + timedelta(hours=3)
    reminders.sweep(admin_session, tenant_id=tenant_a.id, now=later)
    admin_session.commit()
    rows = _log_rows(admin_session, tenant_a, person)
    assert all(r.status == "sent" for r in rows)
    assert len(_outbox_rows(admin_session, tenant_a)) == before + 1

def test_resend_requeues_and_audits(current_account):
    # The command takes a genuine session-cookie Request; an actor id is never authority.
    a = current_account
    _allow(a)
    log = ReminderLog(tenant_id=a.tid, person_id=a.pid, event_kind="graded",
                      occurrence_key=f"resend:{uuid4()}", title="Resend me", link="/progress", status="queued")
    a.admin.add(log)
    a.admin.commit()
    log_id = log.id
    before = len(_outbox_rows(a.admin, a.tenant))
    reminders.resend(a.db, request=_request(a.tenant, a.token), tenant_id=a.tid, log_id=log_id)
    a.db.commit()
    a.admin.expire_all()
    assert len(_outbox_rows(a.admin, a.tenant)) == before + 1
    events = list_events(a.admin, tenant_id=a.tid, action="reminder.resend")
    assert events and str(log_id) in (events[0].entity_id or "")

# ---------------------------------------------------------------------------
# Inactivity nudges escalate, then stop
# ---------------------------------------------------------------------------

def _backdate_enrolment(admin_session, tenant, person, *, days):
    """Make the learner look dormant since `days` ago with no ledger activity."""
    admin_session.execute(
        Enrollment.__table__.update()
        .where(Enrollment.tenant_id == tenant.id)
        .where(Enrollment.person_id == person.id)
        .values(created_at=datetime.now(UTC) - timedelta(days=days))
    )
    admin_session.commit()

def _inactivity_keys(db, tenant, person):
    return sorted(
        r.occurrence_key for r in _log_rows(db, tenant, person) if r.event_kind == "inactivity"
    )

def test_inactivity_nudge_repeats_once_per_window(admin_session, tenant_a):
    """Regression: the occurrence key was the bare anchor date, which never moves
    for a dormant learner, so ReminderLog suppressed every sweep after the first.
    192 nudges went out in July 2026 and none afterwards."""
    person, *_ = _seed(admin_session, tenant_a, email="inact-esc@a.edu")
    _backdate_enrolment(admin_session, tenant_a, person, days=40)
    base = datetime.now(UTC)

    # Three sweeps a week apart: each elapsed window is its own occurrence.
    for week in range(3):
        reminders.sweep(admin_session, tenant_id=tenant_a.id, now=base + timedelta(days=7 * week))
    admin_session.commit()

    keys = _inactivity_keys(admin_session, tenant_a, person)
    assert len(keys) == 3, keys
    assert len(set(keys)) == 3  # distinct occurrences, not one repeated
    assert all(":w" in k for k in keys)

def test_inactivity_nudge_fires_once_within_a_single_window(admin_session, tenant_a):
    """Escalation must not become a nudge per sweep — the timer runs every 5 min."""
    person, *_ = _seed(admin_session, tenant_a, email="inact-same@a.edu")
    _backdate_enrolment(admin_session, tenant_a, person, days=10)
    base = datetime.now(UTC)

    for minutes in (0, 5, 10, 60):
        reminders.sweep(admin_session, tenant_id=tenant_a.id, now=base + timedelta(minutes=minutes))
    admin_session.commit()

    assert len(_inactivity_keys(admin_session, tenant_a, person)) == 1

def test_inactivity_nudges_stop_at_the_cap(admin_session, tenant_a):
    """Bounded on purpose: a learner who has ignored four is telling us something."""
    person, *_ = _seed(admin_session, tenant_a, email="inact-cap@a.edu")
    _backdate_enrolment(admin_session, tenant_a, person, days=40)
    base = datetime.now(UTC)

    for week in range(10):  # far past the cap of 4
        reminders.sweep(admin_session, tenant_id=tenant_a.id, now=base + timedelta(days=7 * week))
    admin_session.commit()

    keys = _inactivity_keys(admin_session, tenant_a, person)
    assert len(keys) == reminders.DEFAULT_INACTIVITY_MAX_NUDGES

def test_long_dormant_learner_is_still_reachable(admin_session, tenant_a):
    """The cap counts nudges already sent for the spell, not the window number.
    A learner dormant since before this change has a high window number but few
    nudges, and must not be written off by arithmetic."""
    person, *_ = _seed(admin_session, tenant_a, email="inact-old@a.edu")
    _backdate_enrolment(admin_session, tenant_a, person, days=400)

    reminders.sweep(admin_session, tenant_id=tenant_a.id, now=datetime.now(UTC))
    admin_session.commit()

    assert len(_inactivity_keys(admin_session, tenant_a, person)) == 1

def test_final_inactivity_nudge_says_it_is_the_last(admin_session, tenant_a):
    """Silence after the cap should read as a decision, not as losing interest."""
    person, *_ = _seed(admin_session, tenant_a, email="inact-final@a.edu")
    _backdate_enrolment(admin_session, tenant_a, person, days=40)
    base = datetime.now(UTC)

    for week in range(reminders.DEFAULT_INACTIVITY_MAX_NUDGES):
        reminders.sweep(admin_session, tenant_id=tenant_a.id, now=base + timedelta(days=7 * week))
    admin_session.commit()

    titles = [
        r.title for r in _log_rows(admin_session, tenant_a, person) if r.event_kind == "inactivity"
    ]
    assert len(titles) == reminders.DEFAULT_INACTIVITY_MAX_NUDGES
    assert sum("Last reminder" in t for t in titles) == 1
    assert "Last reminder" in titles[-1]

def test_active_learner_draws_no_inactivity_nudge(admin_session, tenant_a):
    person, *_ = _seed(admin_session, tenant_a, email="inact-active@a.edu")
    _backdate_enrolment(admin_session, tenant_a, person, days=1)

    reminders.sweep(admin_session, tenant_id=tenant_a.id, now=datetime.now(UTC))
    admin_session.commit()

    assert _inactivity_keys(admin_session, tenant_a, person) == []
