"""Hosted actual reminder reads, ledger/outbox atomicity and replay. Authored NOT RUN.

Requires installed canonical owners and future a004 FKs; no mock replaces a
Learning read, notification owner, reminder ledger or outbox enqueue.
"""
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from dotmac_learning import CourseWrite, create_course
from dotmac_learning.participation_models import Cohort, Enrollment
from sqlalchemy import select

from app.models.email_outbox import EmailOutbox
from app.models.notification import Notification
from app.models.reminder import ReminderLog
from app.services import reminders, settings_store
from app.services.api_identity_reads import certificate_recipient
from legacy_probe.services.test_canonical_current_user import current_account

assert current_account


def _seed(a):
    now = datetime.now(UTC)
    cohort = Cohort(tenant_id=a.tid, name=uuid4().hex, discipline="reminder")
    a.admin.add(cohort)
    a.admin.flush()
    enrollment = Enrollment(tenant_id=a.tid, cohort_id=cohort.id, person_id=a.pid,
        role_in_cohort="student", status="active", created_at=now)
    a.admin.add(enrollment)
    course = create_course(a.admin, CourseWrite(id=uuid4(), tenant_id=a.tid, slug=uuid4().hex,
        title="Reminder privacy <script>", discipline="reminder", source_ref="hosted", status="draft", listed=False))
    a.admin.commit()
    return now, enrollment, course


def _logs(db, a):
    return db.scalars(select(ReminderLog).where(ReminderLog.tenant_id == a.tid,
                                               ReminderLog.person_id == a.pid)).all()


def _outbox(db, a):
    return db.scalars(select(EmailOutbox).where(EmailOutbox.tenant_id == a.tid,
                                               EmailOutbox.kind.like("reminder_%"))).all()


def test_actual_learning_detection_idempotence_and_root_rollback(current_account):
    a = current_account
    now, enrollment, _course = _seed(a)
    event_key = f"course_assigned:{enrollment.id}"
    events = reminders._detect_events(a.db, tenant_id=a.tid, person_id=a.pid, now=now, inactivity_days=7)
    assert any(event["key"] == event_key for event in events)
    root = a.db.get_transaction()
    reminders.sweep(a.db, tenant_id=a.tid, now=now)
    first = [row for row in _logs(a.db, a) if row.occurrence_key == event_key]
    assert len(first) == 1
    assert any(row.idempotency_key == first[0].outbox_key and row.recipient == a.email for row in _outbox(a.db, a))
    assert a.db.scalar(select(Notification).where(Notification.person_id == a.pid,
                                                 Notification.title == first[0].title)) is not None
    reminders.sweep(a.db, tenant_id=a.tid, now=now + timedelta(minutes=5))
    assert len([row for row in _logs(a.db, a) if row.occurrence_key == event_key]) == 1
    assert a.db.get_transaction() is root
    outbox_key = first[0].outbox_key
    a.db.rollback()
    assert a.admin.scalar(select(ReminderLog).where(ReminderLog.tenant_id == a.tid,
                                                  ReminderLog.occurrence_key == event_key)) is None
    assert a.admin.scalar(select(EmailOutbox).where(EmailOutbox.tenant_id == a.tid,
                                                   EmailOutbox.idempotency_key == outbox_key)) is None


def test_queued_digest_replay_uses_actual_outbox_and_contact_privacy(current_account):
    a = current_account
    now, enrollment, _course = _seed(a)
    pref = reminders.save_preference(a.db, tenant_id=a.tid, person_id=a.pid,
        frequency="weekly_digest", quiet_start_hour=None, quiet_end_hour=None, optouts=[])
    # Pick a non-digest instant using the actual configured local boundary.
    cfg = settings_store.effective(a.db)
    while reminders._digest_due(pref, now, hour=int(cfg.reminder_digest_hour), weekday=int(cfg.reminder_digest_weekday)):
        now += timedelta(hours=1)
    reminders.sweep(a.db, tenant_id=a.tid, now=now)
    rows = [row for row in _logs(a.db, a) if row.occurrence_key == f"course_assigned:{enrollment.id}"]
    assert len(rows) == 1 and rows[0].status == "queued"
    person = certificate_recipient(a.db, tenant_id=a.tid, person_id=a.pid)
    assert reminders._flush_queued(a.db, tenant_id=a.tid, person=person,
        branding="Academy", base_url="https://example.test", now=now) >= 1
    first_keys = {row.idempotency_key for row in _outbox(a.db, a)}
    assert reminders._flush_queued(a.db, tenant_id=a.tid, person=person,
        branding="Academy", base_url="https://example.test", now=now) == 0
    assert {row.idempotency_key for row in _outbox(a.db, a)} == first_keys
    assert rows[0].status == "sent"
    assert all(row.payload == {} and row.recipient == a.email for row in _outbox(a.db, a))
    a.db.rollback()
