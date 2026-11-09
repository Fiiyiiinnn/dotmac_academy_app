"""Proposed hosted invocation canaries. AUTHORED NOT RUN; never import locally.

These call the actual command and real owners. Fault wrappers call the genuine
owner first; neither authorization nor its data owner is replaced by a no-op.
Requires the candidate's genuine hosted fixture/owner/FK composition.
"""
from uuid import uuid4

import pytest
from dotmac_kernel.audit import AuditEvent
from dotmac_kernel.models import Party, PartyPerson, PartyType
from dotmac_kernel.session_runtime import resolve_database_runtime
from sqlalchemy import select, update

from app.models.email_outbox import EmailOutbox
from app.models.reminder import ReminderLog
from app.services import reminders
from app.services.reminder_resend_unit import ReminderResendError
from legacy_probe.services.test_canonical_current_user import _request, current_account
from legacy_probe.services.test_reminder_resend_request_owner import _log
from legacy_probe.web.test_product_admin_routes_owner import _allow, _restore_policy

assert current_account and _restore_policy


def test_swallowed_physical_commit_attempt_cannot_escape_root(current_account, monkeypatch):
    a = current_account
    _allow(a)
    lid = _log(a)
    import app.services.reminder_resend as command
    actual_enqueue = command.enqueue_email

    def completing_enqueue(db, **kwargs):
        queued = actual_enqueue(db, **kwargs)
        try:
            db.connection().commit()
        except ReminderResendError as exc:
            # A guard must latch this denied completion even if a participant
            # catches it; a post-body identity check alone cannot undo COMMIT.
            assert isinstance(exc, ReminderResendError)
        return queued

    monkeypatch.setattr(command, 'enqueue_email', completing_enqueue)
    with pytest.raises(ReminderResendError):
        reminders.resend(a.db, request=_request(a.tenant, a.token), tenant_id=a.tid, log_id=lid)
    a.db.rollback()
    a.admin.expire_all()
    assert a.admin.get(ReminderLog, lid).status == 'queued'
    assert a.admin.scalar(select(EmailOutbox.id).where(
        EmailOutbox.tenant_id == a.tid,
        EmailOutbox.idempotency_key.like(f'reminder_resend:{lid}:%'),
    )) is None


def test_actual_audit_plus_unexpected_target_write_rolls_back(current_account, monkeypatch):
    a = current_account
    _allow(a)
    lid = _log(a)
    original = a.admin.get(ReminderLog, lid).title
    import app.services.reminder_resend as command
    actual_write = command.write_audit_event

    def mutating_write(db, **kwargs):
        audit = actual_write(db, **kwargs)
        db.execute(update(ReminderLog).where(
            ReminderLog.tenant_id == a.tid, ReminderLog.id == lid,
        ).values(title='UNAUTHORIZED replacement'))
        return audit

    monkeypatch.setattr(command, 'write_audit_event', mutating_write)
    with pytest.raises(ReminderResendError):
        reminders.resend(a.db, request=_request(a.tenant, a.token), tenant_id=a.tid, log_id=lid)
    # Catch at the real caller boundary and commit unrelated caller work: the
    # whole command must already have rolled back its unexpected effects.
    a.db.commit()
    a.admin.expire_all()
    assert a.admin.get(ReminderLog, lid).title == original
    assert a.admin.get(ReminderLog, lid).status == 'queued'
    assert a.admin.scalar(select(AuditEvent.id).where(
        AuditEvent.tenant_id == a.tid, AuditEvent.entity_id == str(lid),
        AuditEvent.action == 'reminder.resend',
    )) is None


def test_canonical_contact_without_email_cannot_claim_outbox_success(current_account):
    a = current_account
    _allow(a)
    recipient = Party(tenant_id=a.tid, party_type=PartyType.person,
                      display_name='Historical no-email person', email=None, is_active=False)
    a.admin.add(recipient)
    a.admin.flush()
    a.admin.add(PartyPerson(party_id=recipient.id, first_name='Historical', last_name=''))
    a.admin.flush()
    log = ReminderLog(tenant_id=a.tid, person_id=recipient.id, event_kind='graded',
                      occurrence_key=f'no-email:{uuid4()}', title='Grade', link='/progress', status='queued')
    a.admin.add(log)
    a.admin.commit()
    lid = log.id
    with pytest.raises(ValueError):
        reminders.resend(a.db, request=_request(a.tenant, a.token), tenant_id=a.tid, log_id=lid)
    a.db.commit()
    a.admin.expire_all()
    assert a.admin.get(ReminderLog, lid).status == 'queued'
    assert a.admin.scalar(select(AuditEvent.id).where(
        AuditEvent.tenant_id == a.tid, AuditEvent.entity_id == str(lid),
        AuditEvent.action == 'reminder.resend',
    )) is None


@pytest.mark.parametrize("surface", ["session", "session_root", "physical_root", "savepoint", "physical_savepoint"])
def test_swallowed_completion_surfaces_refuse_before_effect(current_account, monkeypatch, surface):
    a = current_account
    _allow(a)
    lid = _log(a)
    import app.services.reminder_resend as command
    actual_enqueue = command.enqueue_email
    def completing_enqueue(db, **kwargs):
        queued = actual_enqueue(db, **kwargs)
        targets = {"session": db, "session_root": db.get_transaction(),
                   "physical_root": db.connection().get_transaction(),
                   "savepoint": db.get_nested_transaction(),
                   "physical_savepoint": db.connection().get_nested_transaction()}
        try:
            targets[surface].commit()
        except ReminderResendError as exc:
            assert isinstance(exc, ReminderResendError)
        return queued
    monkeypatch.setattr(command, "enqueue_email", completing_enqueue)
    with pytest.raises(ReminderResendError):
        reminders.resend(a.db, request=_request(a.tenant, a.token), tenant_id=a.tid, log_id=lid)
    a.db.rollback()
    a.admin.expire_all()
    assert a.admin.get(ReminderLog, lid).status == "queued"
    assert a.admin.scalar(select(EmailOutbox.id).where(EmailOutbox.tenant_id == a.tid,
        EmailOutbox.idempotency_key.like(f"reminder_resend:{lid}:%"))) is None


def test_dirty_target_after_return_refuses_caller_commit_and_requires_rollback(current_account):
    a = current_account
    _allow(a)
    lid = _log(a)
    log = reminders.resend(a.db, request=_request(a.tenant, a.token), tenant_id=a.tid, log_id=lid)
    original = log.title
    log.title = "dirty after successful command"
    with pytest.raises(ReminderResendError):
        a.db.commit()
    # Even a caller that catches completion refusal cannot subsequently commit.
    log.title = original
    with pytest.raises(ReminderResendError):
        a.db.commit()
    a.db.rollback()
    a.admin.expire_all()
    assert a.admin.get(ReminderLog, lid).title == original
    assert a.admin.get(ReminderLog, lid).status == "queued"
    assert a.admin.scalar(select(AuditEvent.id).where(AuditEvent.tenant_id == a.tid,
        AuditEvent.entity_id == str(lid), AuditEvent.action == "reminder.resend")) is None


def test_normal_owner_commit_and_same_root_idempotent_resend(current_account, monkeypatch):
    from datetime import UTC, datetime
    a = current_account
    _allow(a)
    lid = _log(a)
    import app.services.reminder_resend as command
    instant = datetime.now(UTC)
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant
    monkeypatch.setattr(command, "datetime", FixedDatetime)
    first = reminders.resend(a.db, request=_request(a.tenant, a.token), tenant_id=a.tid, log_id=lid)
    key = first.outbox_key
    second = reminders.resend(a.db, request=_request(a.tenant, a.token), tenant_id=a.tid, log_id=lid)
    assert second.id == first.id and second.outbox_key == key
    assert len(a.db.scalars(select(EmailOutbox).where(EmailOutbox.tenant_id == a.tid,
        EmailOutbox.idempotency_key == key)).all()) == 1
    assert len(a.db.scalars(select(AuditEvent).where(AuditEvent.tenant_id == a.tid,
        AuditEvent.entity_id == str(lid), AuditEvent.action == "reminder.resend")).all()) == 2
    a.db.commit()
    a.admin.expire_all()
    assert a.admin.get(ReminderLog, lid).status == "sent"
    assert a.admin.scalar(select(EmailOutbox.id).where(EmailOutbox.tenant_id == a.tid,
        EmailOutbox.idempotency_key == key)) is not None
    # A new caller transaction is free of stale guards and participant pins.
    resolve_database_runtime().set_tenant(a.db, a.tid)
    a.db.commit()
