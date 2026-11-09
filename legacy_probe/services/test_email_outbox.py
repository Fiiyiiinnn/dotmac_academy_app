"""Durable email intent, retry, and reconciliation contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.models.email_outbox import EmailOutbox
from app.services import email_outbox
from app.services.email import EmailResult


def _enqueue(db, tenant_id, *, key="message-1", kind="generic", payload=None):
    return email_outbox.enqueue_email(
        db,
        tenant_id=tenant_id,
        idempotency_key=key,
        kind=kind,
        recipient="learner@example.com",
        subject="Academy update",
        html_body="<p>Hello</p>",
        text_body="Hello",
        payload=payload,
    )


def test_enqueue_is_idempotent_in_the_caller_transaction(admin_session, tenant_a):
    # True means "this call created the row"; the duplicate returns False so a
    # caller counting sends reports one send, not two attempts.
    assert _enqueue(admin_session, tenant_a.id) is True
    assert _enqueue(admin_session, tenant_a.id) is False

    count = admin_session.scalar(
        select(func.count())
        .select_from(EmailOutbox)
        .where(EmailOutbox.tenant_id == tenant_a.id)
        .where(EmailOutbox.idempotency_key == "message-1")
    )
    assert count == 1
    admin_session.rollback()


def test_delivery_records_retry_then_terminal_failure(
    admin_session,
    tenant_a,
    monkeypatch,
):
    _enqueue(
        admin_session,
        tenant_a.id,
        key="failed-account-invite",
        kind="account_invite",
    )
    row = admin_session.scalars(
        select(EmailOutbox).where(EmailOutbox.tenant_id == tenant_a.id)
    ).one()
    now = datetime.now(UTC)

    monkeypatch.setattr(
        email_outbox,
        "send_email_detailed",
        lambda *args, **kwargs: EmailResult(False, "SMTPAuthenticationError"),
    )

    first = email_outbox.deliver_pending(admin_session, now=now)
    assert first == {"sent": 0, "retried": 1, "failed": 0}
    assert row.status == "pending"
    assert row.attempts == 1
    assert row.available_at == now + timedelta(minutes=2)
    assert row.last_error == "SMTPAuthenticationError"
    admin_session.commit()  # next retry is a later worker transaction

    row.attempts = email_outbox.MAX_ATTEMPTS - 1
    row.available_at = now - timedelta(seconds=1)
    admin_session.flush()
    terminal = email_outbox.deliver_pending(admin_session, now=now)
    assert terminal == {"sent": 0, "retried": 0, "failed": 1}
    assert row.status == "failed"
    assert row.attempts == email_outbox.MAX_ATTEMPTS

    assert email_outbox.requeue_failed(admin_session, tenant_id=tenant_a.id) == 1
    assert row.status == "pending"
    assert row.attempts == 0
    assert row.last_error is None
    admin_session.rollback()


def test_certificate_missing_state_retries_without_sending(
    admin_session,
    tenant_a,
    monkeypatch,
):
    _enqueue(
        admin_session,
        tenant_a.id,
        kind="certificate",
        payload={"person_id": "not-a-uuid"},
    )
    calls = []
    monkeypatch.setattr(
        email_outbox,
        "send_email_detailed",
        lambda *args, **kwargs: calls.append(args) or EmailResult(True),
    )

    result = email_outbox.deliver_pending(admin_session, now=datetime.now(UTC))
    row = admin_session.scalars(
        select(EmailOutbox).where(EmailOutbox.tenant_id == tenant_a.id)
    ).one()
    assert result == {"sent": 0, "retried": 1, "failed": 0}
    assert calls == []
    assert row.last_error == "ValueError"
    admin_session.rollback()


def test_success_uses_stable_message_id_and_marks_sent(
    admin_session,
    tenant_a,
    monkeypatch,
):
    _enqueue(admin_session, tenant_a.id)
    captured = {}

    def fake_send(*args, **kwargs):
        captured.update(kwargs)
        return EmailResult(True)

    monkeypatch.setattr(email_outbox, "send_email_detailed", fake_send)
    now = datetime.now(UTC)
    result = email_outbox.deliver_pending(admin_session, now=now)
    row = admin_session.scalars(
        select(EmailOutbox).where(EmailOutbox.tenant_id == tenant_a.id)
    ).one()

    assert result == {"sent": 1, "retried": 0, "failed": 0}
    assert row.status == "sent"
    assert row.sent_at == now
    assert captured["message_id"] == f"<academy-outbox-{row.id}@dotmac.local>"
    admin_session.rollback()
