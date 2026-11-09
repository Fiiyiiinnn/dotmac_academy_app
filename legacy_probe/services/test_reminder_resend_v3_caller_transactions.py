"""Genuine V2 caller API regression canaries. AUTHORED NOT RUN.

Uses actual command, canonical cookie/grants, public owners and SQLAlchemy's
public caller APIs. No owner or authorization implementation is replaced.
"""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from dotmac_kernel.audit import AuditEvent
from dotmac_kernel.session_runtime import resolve_database_runtime
from sqlalchemy import event, select, update
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import sessionmaker

from app.models.email_outbox import EmailOutbox
from app.models.reminder import ReminderLog
from app.services import notifications, reminders
from app.services.reminder_resend_unit import ReminderResendError
from legacy_probe.services.test_canonical_current_user import _request, current_account
from legacy_probe.services.test_reminder_resend_request_owner import _log
from legacy_probe.web.test_product_admin_routes_owner import _allow, _restore_policy

assert current_account and _restore_policy


def test_caller_begin_context_commits_successful_resend(current_account):
    a = current_account
    _allow(a)
    lid = _log(a)
    a.db.commit()
    with a.db.begin():
        resolve_database_runtime().set_tenant(a.db, a.tid)
        log = reminders.resend(a.db, request=_request(a.tenant, a.token), tenant_id=a.tid, log_id=lid)
        key = log.outbox_key
    a.admin.expire_all()
    assert a.admin.get(ReminderLog, lid).status == 'sent'
    assert a.admin.scalar(select(EmailOutbox.id).where(
        EmailOutbox.tenant_id == a.tid, EmailOutbox.idempotency_key == key,
    )) is not None


def test_caller_begin_context_rolls_back_without_masking_original_error(current_account):
    class CallerAbort(Exception):
        pass
    a = current_account
    _allow(a)
    lid = _log(a)
    a.db.commit()
    with pytest.raises(CallerAbort):
        with a.db.begin():
            resolve_database_runtime().set_tenant(a.db, a.tid)
            reminders.resend(a.db, request=_request(a.tenant, a.token), tenant_id=a.tid, log_id=lid)
            raise CallerAbort()
    assert a.db.get_transaction() is None
    a.admin.expire_all()
    assert a.admin.get(ReminderLog, lid).status == 'queued'
    assert a.admin.scalar(select(AuditEvent.id).where(
        AuditEvent.tenant_id == a.tid, AuditEvent.action == 'reminder.resend', AuditEvent.entity_id == str(lid),
    )) is None


def test_caller_driver_select_after_return_keeps_valid_commit(current_account):
    a = current_account
    _allow(a)
    lid = _log(a)
    reminders.resend(a.db, request=_request(a.tenant, a.token), tenant_id=a.tid, log_id=lid)
    assert a.db.connection().exec_driver_sql('SELECT 1').scalar_one() == 1
    a.db.commit()
    a.admin.expire_all()
    assert a.admin.get(ReminderLog, lid).status == 'sent'


def test_caller_enclosing_savepoint_completes_then_root_commits(current_account):
    a = current_account
    _allow(a)
    lid = _log(a)
    a.db.commit()
    with a.db.begin():
        resolve_database_runtime().set_tenant(a.db, a.tid)
        with a.db.begin_nested():
            reminders.resend(a.db, request=_request(a.tenant, a.token), tenant_id=a.tid, log_id=lid)
    a.admin.expire_all()
    assert a.admin.get(ReminderLog, lid).status == 'sent'




def _invoke(a, lid):
    return reminders.resend(a.db, request=_request(a.tenant, a.token), tenant_id=a.tid, log_id=lid)


def _observe(a, lid, status, audits=None):
    a.admin.rollback()
    a.admin.expire_all()
    assert a.admin.get(ReminderLog, lid).status == status
    rows = a.admin.scalars(select(AuditEvent).where(AuditEvent.tenant_id == a.tid,
        AuditEvent.entity_id == str(lid), AuditEvent.action == "reminder.resend")).all()
    if audits is not None:
        assert len(rows) == audits
    return rows


@pytest.mark.parametrize("entry", ["session", "logical_root", "connection", "physical_root"])
def test_caller_root_commit_entries_use_actual_durable_readback(current_account, entry):
    a = current_account
    _allow(a)
    lid = _log(a)
    log = _invoke(a, lid)
    key = log.outbox_key
    targets = {"session": a.db, "logical_root": a.db.get_transaction(),
               "connection": a.db.connection(), "physical_root": a.db.connection().get_transaction()}
    targets[entry].commit()
    _observe(a, lid, "sent", 1)
    assert a.admin.scalar(select(EmailOutbox.id).where(EmailOutbox.tenant_id == a.tid,
        EmailOutbox.idempotency_key == key)) is not None
    # Physical entry leaves native stale logical handles; caller reconciles them.
    a.db.rollback()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert a.db.scalar(select(ReminderLog.id).where(ReminderLog.id == lid)) == lid
    a.db.commit()


@pytest.mark.parametrize("entry", ["session_rollback", "root_rollback", "session_close", "session_invalidate",
                                   "connection_rollback", "connection_close", "connection_invalidate",
                                   "physical_rollback", "physical_close"])
def test_caller_root_abort_entries_never_accept_receipts(current_account, entry):
    a = current_account
    _allow(a)
    lid = _log(a)
    _invoke(a, lid)
    targets = {"session": a.db, "root": a.db.get_transaction(), "connection": a.db.connection(),
               "physical": a.db.connection().get_transaction()}
    kind, method = entry.split("_", 1)
    getattr(targets[kind], method)()
    _observe(a, lid, "queued", 0)
    a.db.rollback()


@pytest.mark.parametrize("participant", ["enqueue_email", "write_audit_event"])
@pytest.mark.parametrize("surface", ["session", "connection", "logical_root", "physical_root",
                                     "logical_helper", "physical_helper", "caller_ancestor"])
@pytest.mark.parametrize("operation", ["commit", "rollback", "close"])
def test_active_participant_completion_precedes_original_side_effect(current_account, monkeypatch,
        participant, surface, operation):
    a = current_account
    _allow(a)
    lid = _log(a)
    ancestor = a.db.begin_nested()
    # Materialize and capture the actual ancestor before installing the command.
    a.db.connection()
    import app.services.reminder_resend as command
    actual = getattr(command, participant)
    attempts = []
    def attempting(db, **kwargs):
        result = actual(db, **kwargs)
        targets = {"session": db, "connection": db.connection(), "logical_root": db.get_transaction(),
                   "physical_root": db.connection().get_transaction(), "logical_helper": db.get_nested_transaction(),
                   "physical_helper": db.connection().get_nested_transaction(), "caller_ancestor": ancestor}
        try:
            getattr(targets[surface], operation)()
        except ReminderResendError:
            attempts.append("refused-before-original")
        return result
    monkeypatch.setattr(command, participant, attempting)
    with pytest.raises(ReminderResendError):
        _invoke(a, lid)
    assert attempts == ["refused-before-original"]
    assert ancestor.is_active and a.db.get_transaction().is_active
    ancestor.rollback()
    a.db.commit()
    _observe(a, lid, "queued", 0)


def test_two_preexisting_ancestors_release_inner_then_abort_outer(current_account):
    a = current_account
    _allow(a)
    lid = _log(a)
    unrelated = notifications.notify(a.db, tenant_id=a.tid, person_id=a.pid, kind="caller",
        title="outside nested resend")
    uid = unrelated.id
    outer = a.db.begin_nested()
    inner = a.db.begin_nested()
    _invoke(a, lid)
    inner.commit()
    outer.rollback()
    a.db.commit()
    _observe(a, lid, "queued", 0)
    from app.models.notification import Notification
    assert a.admin.get(Notification, uid) is not None


@pytest.mark.parametrize("abort", [False, True])
def test_caller_nesting_created_after_return_preserves_earlier_claims(current_account, abort):
    a = current_account
    _allow(a)
    lid = _log(a)
    _invoke(a, lid)
    with pytest.raises(InvalidRequestError):
        a.db.begin()  # Native begin-on-active error must not poison the root.
    nested = a.db.begin_nested()
    row = notifications.notify(a.db, tenant_id=a.tid, person_id=a.pid, kind="caller", title="later nested")
    uid = row.id
    if abort:
        nested.rollback()
    else:
        nested.commit()
    a.db.commit()
    _observe(a, lid, "sent", 1)
    from app.models.notification import Notification
    assert (a.admin.get(Notification, uid) is None) == abort


def _two_instants(monkeypatch):
    import app.services.reminder_resend as command
    instant = datetime.now(UTC)
    moments = iter((instant, instant + timedelta(seconds=1)))
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return next(moments)
    monkeypatch.setattr(command, "datetime", Clock)


@pytest.mark.parametrize("abort", [False, True])
def test_nested_resend_supersession_restores_sealed_predecessor(current_account, monkeypatch, abort):
    a = current_account
    _allow(a)
    lid = _log(a)
    _two_instants(monkeypatch)
    first = _invoke(a, lid)
    old_key = first.outbox_key
    nested = a.db.begin_nested()
    second = _invoke(a, lid)
    new_key = second.outbox_key
    assert old_key != new_key
    if abort:
        nested.rollback()
        # SQLAlchemy's native rollback expiration must resolve to sealed A.
        assert first.outbox_key == old_key
    else:
        nested.commit()
    a.db.commit()
    _observe(a, lid, "sent", 1 if abort else 2)
    row = a.admin.get(ReminderLog, lid)
    assert row.outbox_key == (old_key if abort else new_key)
    keys = set(a.admin.scalars(select(EmailOutbox.idempotency_key).where(
        EmailOutbox.tenant_id == a.tid, EmailOutbox.idempotency_key.in_([old_key, new_key]))).all())
    assert keys == ({old_key} if abort else {old_key, new_key})


def test_failed_second_command_keeps_first_receipt_and_unrelated_work(current_account, monkeypatch):
    a = current_account
    _allow(a)
    lid = _log(a)
    _two_instants(monkeypatch)
    old_key = _invoke(a, lid).outbox_key
    import app.services.reminder_resend as command
    actual = command.write_audit_event
    class CallerFailure(Exception):
        pass
    def failed_owner(db, **kwargs):
        actual(db, **kwargs)
        raise CallerFailure()
    monkeypatch.setattr(command, "write_audit_event", failed_owner)
    with pytest.raises(CallerFailure):
        _invoke(a, lid)
    notifications.notify(a.db, tenant_id=a.tid, person_id=a.pid, kind="caller", title="after failure")
    a.db.commit()
    _observe(a, lid, "sent", 1)
    assert a.admin.get(ReminderLog, lid).outbox_key == old_key


@pytest.mark.parametrize("failure", ["no_sql", "raise"])
def test_unproven_physical_nested_rollback_cannot_retire_receipts(current_account, monkeypatch, failure):
    a = current_account
    _allow(a)
    lid = _log(a)
    nested = a.db.begin_nested()
    outbox_key = _invoke(a, lid).outbox_key
    dialect = a.db.connection().dialect
    actual = dialect.do_rollback_to_savepoint
    class RecoveryFailure(Exception):
        pass
    def broken(connection, name):
        if failure == "raise":
            raise RecoveryFailure()
        return None  # Actual DB savepoint remains: fixed baseline must detect it.
    monkeypatch.setattr(dialect, "do_rollback_to_savepoint", broken)
    with pytest.raises((RecoveryFailure, ReminderResendError)):
        nested.rollback()
    monkeypatch.setattr(dialect, "do_rollback_to_savepoint", actual)
    with pytest.raises(ReminderResendError):
        a.db.commit()
    a.db.rollback()
    _observe(a, lid, "queued", 0)
    # Neither the outbox email nor any audit row survived the refused commit.
    assert a.admin.scalar(select(EmailOutbox.id).where(
        EmailOutbox.tenant_id == a.tid, EmailOutbox.idempotency_key == outbox_key)) is None


@pytest.mark.parametrize("fault", ["dirty", "core", "before_commit", "flush", "late_caught_sql"])
def test_final_commit_fence_refuses_actual_tampering(current_account, fault):
    a = current_account
    _allow(a)
    lid = _log(a)
    log = _invoke(a, lid)
    connection = a.db.connection()
    listeners = []
    if fault == "dirty":
        log.title = "unexpected caller mutation"
    elif fault == "core":
        a.db.execute(update(ReminderLog).where(ReminderLog.id == lid).values(title="unexpected Core mutation"))
    else:
        def mutation(*args):
            if fault == "late_caught_sql":
                try:
                    connection.exec_driver_sql("SELECT 1")
                except ReminderResendError:
                    return  # Final pre-driver latch must still refuse COMMIT.
            else:
                a.db.execute(update(ReminderLog).where(ReminderLog.id == lid).values(title="callback mutation"))
        if fault == "late_caught_sql":
            event.listen(connection, "commit", mutation)
            listeners.append((connection, "commit", mutation))
        elif fault == "flush":
            caller_row = notifications.notify(a.db, tenant_id=a.tid, person_id=a.pid,
                kind="caller", title="before commit flush")
            event.listen(a.db, "after_flush_postexec", mutation)
            listeners.append((a.db, "after_flush_postexec", mutation))
            caller_row.title = "pending ordinary caller flush"
        else:
            event.listen(a.db, "before_commit", mutation)
            listeners.append((a.db, "before_commit", mutation))
    try:
        with pytest.raises(ReminderResendError):
            a.db.commit()
    finally:
        for target, name, listener in listeners:
            event.remove(target, name, listener)
        a.db.rollback()
    _observe(a, lid, "queued", 0)


@pytest.mark.parametrize("raw", ["COMMIT", "  ROLLBACK", "/*comment*/COMMIT", "SELECT 1; COMMIT", "SELECT 1 -- comment"])
@pytest.mark.parametrize("driver", [False, True])
def test_text_control_disguises_fail_before_effect(current_account, raw, driver):
    from sqlalchemy import text
    a = current_account
    _allow(a)
    lid = _log(a)
    _invoke(a, lid)
    with pytest.raises(ReminderResendError):
        if driver:
            a.db.connection().exec_driver_sql(raw)
        else:
            a.db.execute(text(raw))
    with pytest.raises(ReminderResendError):
        a.db.commit()
    a.db.rollback()
    _observe(a, lid, "queued", 0)


def test_independent_sessions_and_preexisting_later_patches_cleanup(current_account, tenant_b):
    from legacy_probe.services.roster_request_fixtures import create_roster_actor
    a = current_account
    _grant_id, slug = _allow(a)
    first = _log(a)
    setup = SimpleNamespace(db=a.db, admin=a.admin, tid=tenant_b.id, tenant=tenant_b)
    actor = create_roster_actor(setup, slug)
    a.db.commit()
    Factory = sessionmaker(bind=a.db.get_bind(), autoflush=False)
    with Factory() as other:
        other_account = SimpleNamespace(db=other, admin=a.admin, pid=actor.pid, tid=tenant_b.id,
            tenant=tenant_b, token=actor.request.cookies["session"])
        second = _log(other_account)
        resolve_database_runtime().set_tenant(a.db, a.tid)
        resolve_database_runtime().set_tenant(other, tenant_b.id)
        original = a.db.commit
        calls = []
        def preexisting():
            calls.append("preexisting")
            return original()
        a.db.commit = preexisting
        _invoke(a, first)
        _invoke(other_account, second)
        guarded = a.db.commit
        def later():
            calls.append("later")
            return guarded()
        a.db.commit = later
        a.db.commit()
        assert a.db.commit is later
        _observe(a, first, "sent", 1)
        assert calls == ["later", "preexisting"]
        # Independent Connection still has its own pins after first cleanup.
        assert other.connection().exec_driver_sql("SELECT 1").scalar_one() == 1
        other.commit()
        _observe(other_account, second, "sent", 1)
        resolve_database_runtime().set_tenant(a.db, a.tid)
        a.db.commit()  # Surviving later wrapper calls ended native pass-through.
        assert calls == ["later", "preexisting", "later", "preexisting"]
        a.db.commit = original


@pytest.mark.parametrize("preexisting", [False, True])
@pytest.mark.parametrize("abort", [False, True])
def test_actual_connection_nested_contexts_retain_or_retire_only_contained_claims(current_account, preexisting, abort):
    a = current_account
    _allow(a)
    lid = _log(a)
    connection = a.db.connection()
    nested = connection.begin_nested() if preexisting else None
    _invoke(a, lid)
    if nested is None:
        nested = connection.begin_nested()
    if abort:
        nested.rollback()
    else:
        nested.commit()
    a.db.commit()
    vanished = preexisting and abort
    _observe(a, lid, "queued" if vanished else "sent", 0 if vanished else 1)



def test_returned_rebinding_cannot_transfer_fixed_receipts(current_account):
    a = current_account
    _allow(a)
    lid = _log(a)
    _invoke(a, lid)
    original_bind = a.db.bind
    a.db.bind = a.admin.get_bind()
    try:
        with pytest.raises(ReminderResendError):
            a.db.commit()
    finally:
        a.db.bind = original_bind
    with pytest.raises(ReminderResendError):
        a.db.commit()
    a.db.rollback()
    _observe(a, lid, "queued", 0)
