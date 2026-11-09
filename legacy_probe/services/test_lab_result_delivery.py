"""D8 delivery contracts. Database cases require the hosted Academy assembly."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.email_outbox import EmailOutbox
from app.models.lab_result_delivery import LabResultDelivery
from app.models.notification import Notification
from app.services import email
from app.services import lab_result_delivery as delivery


def _intent(db, tid, **changes):
    values = dict(
        tenant_id=tid, operation_id=uuid4(), instance_id=uuid4(), person_id=uuid4(),
        activity_id=uuid4(), course_id=uuid4(), submission_id=uuid4(), score_id=uuid4(),
        certificate_id=None, first_completion=False, certificate_created=False,
        first_pass=True, score=8., max_score=10., fraction=.8, passed=True,
        status="pending", attempts=0, created_at=datetime.now(UTC),
        next_attempt_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    values.update(changes)
    row = LabResultDelivery(**values)
    db.add(row)
    db.flush()
    return row


def _reads(monkeypatch, row, *, prefs=None):
    person = SimpleNamespace(id=row.person_id, email="private@example.edu", first_name="Private", prefs=prefs)
    activity = SimpleNamespace(id=row.activity_id, title="Confidential lab")
    monkeypatch.setattr(delivery, "_notification_recipient", lambda *a, **kw: person)
    monkeypatch.setattr(delivery, "get_activity", lambda *a, **kw: activity)
    monkeypatch.setattr("app.services.notifications.notify", lambda *a, **kw: None)
    monkeypatch.setattr(email, "has_other_passing_score", lambda *a, **kw: pytest.fail("drain queried prior pass"))
    monkeypatch.setattr("app.services.settings_store.effective", lambda db: SimpleNamespace(email_auto_on_pass=True))


@pytest.mark.parametrize("prefs,expected", [(None, 1), ({}, 1), ({"email_results": None}, 0), ({"email_results": False}, 0)])
def test_stored_first_pass_and_preference_semantics(admin_session, tenant_a, monkeypatch, prefs, expected):
    row = _intent(admin_session, tenant_a.id)
    _reads(monkeypatch, row, prefs=prefs)
    # Two passes arrive before either drain. Only the event-time first pass sends.
    _intent(admin_session, tenant_a.id, person_id=row.person_id, activity_id=row.activity_id, first_pass=False)
    counts = delivery.materialize_pending(admin_session)
    assert counts.delivered == 2
    messages = admin_session.scalars(select(EmailOutbox).where(EmailOutbox.tenant_id == tenant_a.id)).all()
    assert len(messages) == expected
    if messages:
        assert messages[0].idempotency_key == f"score-pass:{row.score_id}"
        assert "80%" in messages[0].text_body
    assert row.status == "delivered"


@pytest.mark.parametrize("target", ["notify", "enqueue_email"])
def test_send_failure_rolls_back_partial_effects_and_logs_no_payload(admin_session, tenant_a, monkeypatch, caplog, target):
    row = _intent(admin_session, tenant_a.id)
    _reads(monkeypatch, row)
    # Include a prior write inside the same intent before the injected failure.
    original = delivery._materialize_one

    def broken(db, intent):
        db.add(EmailOutbox(tenant_id=tenant_a.id, idempotency_key="partial", kind="generic",
                           recipient="private@example.edu", subject="Confidential lab", html_body="private", payload={}))
        db.flush()
        original(db, intent)

    def fail(*a, **kw):
        raise RuntimeError("DETAIL private@example.edu Confidential lab")

    monkeypatch.setattr(delivery, "_materialize_one", broken)
    monkeypatch.setattr(f"app.services.{'notifications' if target == 'notify' else 'email_outbox'}.{target}", fail)
    counts = delivery.materialize_pending(admin_session)
    assert counts.retried == 1 and counts.delivered == 0
    assert row.status == "pending" and row.attempts == 1
    assert row.last_error in {"RuntimeError:notify", "RuntimeError:enqueue"}
    assert not admin_session.scalars(select(EmailOutbox).where(EmailOutbox.tenant_id == tenant_a.id)).all()
    assert not admin_session.scalars(select(Notification).where(Notification.tenant_id == tenant_a.id)).all()
    assert all(secret not in caplog.text for secret in ("private@example.edu", "Confidential lab", "DETAIL"))
    assert str(row.tenant_id) in caplog.text and str(row.operation_id) in caplog.text


def test_backoff_terminal_requeue_and_purge_boundary(admin_session, tenant_a, monkeypatch):
    now = datetime.now(UTC)
    retry = _intent(admin_session, tenant_a.id, attempts=5)
    terminal = _intent(admin_session, tenant_a.id, attempts=7)
    def fail(*a):
        raise RuntimeError("private detail")
    monkeypatch.setattr(delivery, "_materialize_one", fail)
    counts = delivery.materialize_pending(admin_session, now=now)
    assert counts.retried == 1 and counts.failed_total == 1
    assert retry.next_attempt_at == now + timedelta(minutes=60)
    assert terminal.status == "failed" and terminal.attempts == 8
    assert delivery.requeue_failed(admin_session, tenant_id=tenant_a.id, now=now) == 1
    assert terminal.status == "pending" and terminal.attempts == 0 and terminal.last_error is None
    old = _intent(admin_session, tenant_a.id, status="delivered", delivered_at=now - timedelta(days=90, seconds=1))
    boundary = _intent(admin_session, tenant_a.id, status="delivered", delivered_at=now - timedelta(days=90))
    failed = _intent(admin_session, tenant_a.id, status="failed", delivered_at=now - timedelta(days=100))
    assert delivery.purge_delivered(admin_session, now=now) == 1
    assert admin_session.get(LabResultDelivery, (old.tenant_id, old.operation_id)) is None
    assert admin_session.get(LabResultDelivery, (boundary.tenant_id, boundary.operation_id)) is boundary
    assert admin_session.get(LabResultDelivery, (failed.tenant_id, failed.operation_id)) is failed


def test_legacy_and_stored_first_pass_share_formatter(admin_session, tenant_a, monkeypatch):
    row = _intent(admin_session, tenant_a.id)
    person = SimpleNamespace(id=row.person_id, email="learner@example.edu", first_name="Learner", prefs={})
    activity = SimpleNamespace(id=row.activity_id, title="Lab <one>")
    score = delivery.AcademyDeliveryScore(id=row.score_id, tenant_id=tenant_a.id, score=8., max_score=10., fraction=.8, passed=True)
    captured = []
    monkeypatch.setattr(email, "has_other_passing_score", lambda *a, **kw: False)
    monkeypatch.setattr("app.services.notifications.notify", lambda *a, **kw: None)
    monkeypatch.setattr("app.services.settings_store.effective", lambda db: SimpleNamespace(email_auto_on_pass=True))
    monkeypatch.setattr("app.services.email_outbox.enqueue_email", lambda *a, **kw: captured.append(kw) or True)
    assert email.notify_score_if_first_pass(admin_session, score=score, person=person, activity=activity)
    assert email.notify_score_if_first_pass(admin_session, score=score, person=person, activity=activity, first_pass=True, raise_on_failure=True)
    assert captured[0] == captured[1]


def test_missing_recipient_suppresses_email_without_retry(admin_session, tenant_a, monkeypatch):
    row = _intent(admin_session, tenant_a.id)
    _reads(monkeypatch, row)
    monkeypatch.setattr(delivery, "_notification_recipient", lambda *a, **kw: None)
    counts = delivery.materialize_pending(admin_session)
    assert counts.delivered == 1 and counts.retried == 0
    assert not admin_session.scalars(select(EmailOutbox).where(EmailOutbox.tenant_id == tenant_a.id)).all()


def test_event_time_snapshot_survives_later_score_override(admin_session, tenant_a, monkeypatch):
    row = _intent(admin_session, tenant_a.id)
    _reads(monkeypatch, row)
    # No exact-score API or latest projection is consulted: a later override
    # must neither alter this message nor strand the committed first-pass intent.
    monkeypatch.setattr(email, "has_other_passing_score", lambda *a, **kw: pytest.fail("latest result consulted"))
    counts = delivery.materialize_pending(admin_session)
    message = admin_session.scalars(select(EmailOutbox).where(EmailOutbox.tenant_id == tenant_a.id)).one()
    assert counts.delivered == 1 and "80%" in message.text_body
    assert row.score == 8. and row.passed


def test_two_quick_completions_notify_one_certificate(admin_session, tenant_a, monkeypatch):
    row = _intent(admin_session, tenant_a.id, first_completion=True, certificate_created=True, certificate_id=uuid4(), first_pass=False)
    _intent(admin_session, tenant_a.id, person_id=row.person_id, course_id=row.course_id, certificate_id=row.certificate_id, first_pass=False)
    reads, delivered = [], []
    completion = SimpleNamespace(tenant_id=tenant_a.id, person_id=row.person_id, course_id=row.course_id)
    certificate = SimpleNamespace(id=row.certificate_id)

    def read_completion(db, **keys):
        reads.append(keys)
        return completion

    monkeypatch.setattr(delivery, "get_completion", read_completion)
    monkeypatch.setattr(delivery, "get_certificate", lambda db, **keys: certificate)
    monkeypatch.setattr(delivery, "deliver_completion_notifications", lambda db, result, **kw: delivered.append((result, kw)))
    counts = delivery.materialize_pending(admin_session)
    assert counts.delivered == 2 and len(delivered) == 1
    result, options = delivered[0]
    assert result.first_completion and result.certificate.created
    assert result.certificate.certificate.id == row.certificate_id
    assert options == {"raise_on_failure": True}
    assert reads == [dict(tenant_id=tenant_a.id, person_id=row.person_id, course_id=row.course_id)]


def test_certificate_identity_mismatch_retries_without_effects(admin_session, tenant_a, monkeypatch):
    row = _intent(admin_session, tenant_a.id, first_completion=True, certificate_created=True, certificate_id=uuid4())
    monkeypatch.setattr(delivery, "get_completion", lambda *a, **kw: SimpleNamespace())
    monkeypatch.setattr(delivery, "get_certificate", lambda *a, **kw: SimpleNamespace(id=uuid4()))
    monkeypatch.setattr(delivery, "deliver_completion_notifications", lambda *a, **kw: pytest.fail("wrong certificate delivered"))
    counts = delivery.materialize_pending(admin_session)
    assert counts.retried == 1 and row.attempts == 1 and row.status == "pending"


@pytest.mark.parametrize("target", ["notify", "enqueue_email"])
def test_strict_completion_helpers_roll_back_and_never_log_exception_payload(
    admin_session, tenant_a, monkeypatch, caplog, target
):
    from app.services import completion

    row = _intent(admin_session, tenant_a.id, first_completion=True, certificate_created=True,
                  certificate_id=uuid4(), first_pass=False)
    fact = SimpleNamespace(tenant_id=tenant_a.id, person_id=row.person_id, course_id=row.course_id)
    cert = SimpleNamespace(id=row.certificate_id, tenant_id=tenant_a.id, person_id=row.person_id,
                           course_id=row.course_id, serial="certificate-serial")
    person = SimpleNamespace(email="private@example.edu", first_name="Private", prefs={})
    monkeypatch.setattr(delivery, "get_completion", lambda *a, **kw: fact)
    monkeypatch.setattr(delivery, "get_certificate", lambda *a, **kw: cert)
    monkeypatch.setattr(completion, "get_course", lambda *a, **kw: SimpleNamespace(title="Confidential course"))
    monkeypatch.setattr(completion, "_notification_recipient", lambda *a, **kw: person)
    monkeypatch.setattr("app.services.notifications.notify", lambda *a, **kw: None)

    def fail(*a, **kw):
        raise RuntimeError("DETAIL private@example.edu Confidential course")

    monkeypatch.setattr(f"app.services.{'notifications' if target == 'notify' else 'email_outbox'}.{target}", fail)
    counts = delivery.materialize_pending(admin_session)
    assert counts.retried == 1 and row.status == "pending" and row.attempts == 1
    assert row.last_error == f"RuntimeError:{'notify' if target == 'notify' else 'enqueue'}"
    assert all(secret not in caplog.text for secret in ("DETAIL", "private@example.edu", "Confidential course"))


def test_future_retry_not_claimed_and_delivered_intent_not_replayed(admin_session, tenant_a, monkeypatch):
    now = datetime.now(UTC)
    future = _intent(admin_session, tenant_a.id, next_attempt_at=now + timedelta(minutes=1))
    done = _intent(admin_session, tenant_a.id, status="delivered", delivered_at=now)
    monkeypatch.setattr(delivery, "_materialize_one", lambda *a: pytest.fail("ineligible intent claimed"))
    counts = delivery.materialize_pending(admin_session, now=now)
    assert counts.delivered == counts.retried == 0
    assert future.status == "pending" and done.status == "delivered"
