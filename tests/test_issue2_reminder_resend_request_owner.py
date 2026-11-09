# Maintained port of legacy_probe/services/test_reminder_resend_request_owner.py (issue #2 wave 5).
"""Hosted real reminder command receipts, rollback and lock waits. Authored NOT RUN."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import pytest
from dotmac_kernel.audit import AuditEvent
from dotmac_kernel.models import Party, PartyRoleGrant
from dotmac_kernel.session_runtime import resolve_database_runtime
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from app.models.email_outbox import EmailOutbox
from app.models.reminder import ReminderLog
from app.services import reminders, settings_store
from tests.test_issue2_ical_owner import (
    _request,
    admin_session,
    app_client,
    app_user_session,
    current_account,
    sign_in_account,
    tenant_a,
    tenant_b,
)

assert admin_session and app_client and app_user_session and sign_in_account and tenant_a and tenant_b
from tests.test_issue2_ical_owner import _allow, _bind, _client, _restore_policy, _wait_for_lock  # noqa: E402

assert current_account and _restore_policy


def _log(a):
    log = ReminderLog(
        tenant_id=a.tid,
        person_id=a.pid,
        event_kind="graded",
        occurrence_key=f"hosted:{uuid4()}",
        title="Private <grade>",
        link="/progress",
        status="queued",
    )
    a.admin.add(log)
    a.admin.commit()
    return log.id


def test_actual_post_resend_audit_and_outbox_receipts_rollback(current_account):
    a = current_account
    _allow(a)
    lid = _log(a)
    a.db.scalar(select(ReminderLog.id).where(ReminderLog.id == lid))
    root = a.db.get_transaction()
    response = _client(a).post(f"/admin/reminders/{lid}/resend")
    assert response.status_code == 200 and b"Requeued" in response.content
    log = a.db.get(ReminderLog, lid)
    outbox = a.db.scalars(
        select(EmailOutbox).where(EmailOutbox.tenant_id == a.tid, EmailOutbox.idempotency_key == log.outbox_key)
    ).one()
    audits = a.db.scalars(
        select(AuditEvent).where(
            AuditEvent.tenant_id == a.tid, AuditEvent.action == "reminder.resend", AuditEvent.entity_id == str(lid)
        )
    ).all()
    assert len(audits) == 1 and audits[0].actor_party_id == a.pid
    assert audits[0].actor_type == "user" and audits[0].actor_id == str(a.pid)
    assert audits[0].source_application == "dotmac_academy_app"
    assert audits[0].details == {"event_kind": "graded"}
    assert outbox.recipient == a.email and outbox.payload == {}
    assert "&lt;grade&gt;" in outbox.html_body and "Private <grade>" in outbox.subject
    assert a.email not in response.text and "Private" not in response.text
    assert log.status == "sent" and log.sent_at is not None
    assert a.db.get_transaction() is root
    outbox_key = log.outbox_key
    a.db.rollback()
    a.admin.expire_all()
    assert a.admin.get(ReminderLog, lid).status == "queued"
    assert (
        a.admin.scalar(
            select(EmailOutbox).where(EmailOutbox.tenant_id == a.tid, EmailOutbox.idempotency_key == outbox_key)
        )
        is None
    )
    assert (
        a.admin.scalar(
            select(AuditEvent).where(AuditEvent.entity_id == str(lid), AuditEvent.action == "reminder.resend")
        )
        is None
    )


def test_resend_request_tenant_and_actor_uuid_never_authorize(current_account, tenant_b):
    a = current_account
    _allow(a)
    lid = _log(a)
    for request, tid in [(_request(tenant_b, a.token), a.tid), (_request(a.tenant), a.tid)]:
        with pytest.raises(HTTPException):
            reminders.resend(a.db, request=request, tenant_id=tid, log_id=lid)
    with pytest.raises(TypeError):
        reminders.resend(a.db, tenant_id=a.tid, log_id=lid, actor_person_id=a.pid)
    assert a.db.get(ReminderLog, lid).status == "queued"


def test_post_lock_policy_refusal_rolls_back_actual_enqueue_and_log(current_account, monkeypatch):
    # Inject only a committed policy change AFTER the genuine audit writer;
    # every data owner and authority read still runs its real implementation.
    a = current_account
    _allow(a)
    lid = _log(a)
    import app.services.reminder_resend as command

    real_write = command.write_audit_event

    def changed_policy(db, **kwargs):
        row = real_write(db, **kwargs)
        settings_store.set_many(a.admin, {"reminders_admin_access_roles": "[]"})
        a.admin.commit()
        return row

    monkeypatch.setattr(command, "write_audit_event", changed_policy)
    with pytest.raises(HTTPException) as denied:
        reminders.resend(a.db, request=_request(a.tenant, a.token), tenant_id=a.tid, log_id=lid)
    assert denied.value.status_code == 403
    assert a.db.get(ReminderLog, lid).status == "queued"
    assert (
        a.db.scalar(select(AuditEvent).where(AuditEvent.entity_id == str(lid), AuditEvent.action == "reminder.resend"))
        is None
    )
    assert (
        a.db.scalar(
            select(EmailOutbox).where(
                EmailOutbox.tenant_id == a.tid, EmailOutbox.idempotency_key.like(f"reminder_resend:{lid}:%")
            )
        )
        is None
    )


def test_real_party_wait_then_grant_revocation_refuses_whole_command(current_account):
    a = current_account
    grant_id, _slug = _allow(a)
    lid = _log(a)
    a.db.commit()
    SessionLocal = sessionmaker(bind=a.db.get_bind(), autoflush=False)
    locked, started, release = Event(), Event(), Event()
    backend = []

    def leader():
        with SessionLocal() as session:
            _bind(session, a.tid)
            session.scalars(select(Party).where(Party.tenant_id == a.tid, Party.id == a.pid).with_for_update()).one()
            locked.set()
            assert release.wait(10)
            session.delete(session.get(PartyRoleGrant, grant_id))
            session.commit()

    def follower():
        with SessionLocal() as session:
            _bind(session, a.tid)
            backend.append(session.scalar(text("SELECT pg_backend_pid()")))
            started.set()
            with pytest.raises(HTTPException) as denied:
                reminders.resend(session, request=_request(a.tenant, a.token), tenant_id=a.tid, log_id=lid)
            assert denied.value.status_code == 403
            session.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(leader)
        assert locked.wait(10)
        second = pool.submit(follower)
        assert started.wait(10)
        try:
            resolve_database_runtime().set_tenant(a.db, a.tid)
            _wait_for_lock(a.db, backend[0])
        finally:
            release.set()
        first.result(timeout=20)
        second.result(timeout=20)
    a.db.expire_all()
    assert a.db.get(ReminderLog, lid).status == "queued"
    assert (
        a.db.scalar(select(AuditEvent).where(AuditEvent.entity_id == str(lid), AuditEvent.action == "reminder.resend"))
        is None
    )
    assert (
        a.db.scalar(
            select(EmailOutbox).where(
                EmailOutbox.tenant_id == a.tid, EmailOutbox.idempotency_key.like(f"reminder_resend:{lid}:%")
            )
        )
        is None
    )
