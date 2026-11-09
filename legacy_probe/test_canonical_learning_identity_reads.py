"""Hosted canonical recipient reads; authored here, never run locally."""

import re
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from dotmac_kernel.models import Party, PartyPerson
from dotmac_kernel.session_runtime import resolve_database_runtime
from sqlalchemy import delete, event, text, update

from app.models.identity_extensions import AcademyAccountProfile
from app.services import assessment, completion, email, lab_lifecycle
from app.services.exceptions import ConflictError
from legacy_probe.services.test_canonical_current_user import current_account

assert current_account

# Optionally schema-qualified, optionally quoted parties/party_persons.
_IDENTITY = r'(?:(?:public|"public")\.)?(?:parties|party_persons|"parties"|"party_persons")'
_IDENTITY_WRITE = re.compile(
    rf"\bupdate\s+(?:only\s+)?{_IDENTITY}(?![\w\"])"
    rf"|\binsert\s+into\s+(?:only\s+)?{_IDENTITY}(?![\w\"])"
    rf"|\bdelete\s+from\s+(?:only\s+)?{_IDENTITY}(?![\w\"])"
    rf"|\bmerge\s+into\s+(?:only\s+)?{_IDENTITY}(?![\w\"])"
)
_ROW_LOCK = re.compile(r"\bfor\s+(?:no\s+key\s+)?(?:update|share)\b|\bfor\s+key\s+share\b")


def identity_write_statements(statements):
    """Statements that write, or row-lock, parties/party_persons (case-insensitive)."""
    found = []
    for statement in statements:
        lowered = " ".join(statement.lower().split())
        touches = re.search(rf"(?<![\w\"]){_IDENTITY}(?![\w\"])", lowered) is not None
        if _IDENTITY_WRITE.search(lowered) or (touches and _ROW_LOCK.search(lowered)):
            found.append(lowered[:200])
    return found


@contextmanager
def write_free_identity_read(a):
    """Run an identity read as ``app_user`` in a READ ONLY transaction and prove it write-free.

    The Kernel migration (``_grant_roles``) intentionally grants ``app_user``
    SELECT/INSERT/UPDATE/DELETE on ``parties``/``party_persons``, with tenant
    isolation enforced by RLS, so the absence of an UPDATE privilege is not the
    property to assert. The property is that these read paths never write or lock
    identity rows: PostgreSQL refuses writes and row locks inside a READ ONLY
    transaction (``SET TRANSACTION READ ONLY`` is the transaction's first statement),
    and the SQL text emitted during the read is checked for UPDATE/INSERT/DELETE and
    FOR UPDATE/SHARE on the identity tables. Only statement text is collected,
    never parameters.
    """
    db = a.db
    engine = db.get_bind()
    db.rollback()
    db.execute(text("SET TRANSACTION READ ONLY"))
    resolve_database_runtime().set_tenant(db, a.tid)
    assert db.scalar(text("SELECT current_user")) == "app_user"
    statements: list[str] = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        yield statements
        assert db.scalar(text("SHOW transaction_read_only")) == "on"
    finally:
        event.remove(engine, "before_cursor_execute", capture)
        db.rollback()
    assert any(
        re.search(rf"(?<![\w\"]){_IDENTITY}(?![\w\"])", s.lower()) for s in statements
    ), "the read touched no identity table"
    assert identity_write_statements(statements) == []


def test_recipient_joins_current_fields_and_preferences_without_identity_map_staleness(current_account):
    a = current_account
    cached = (a.db.get(Party, a.pid), a.db.get(PartyPerson, a.pid), a.db.get(AcademyAccountProfile, a.pid))
    changed_email = f"recipient-{uuid4().hex}@a.edu"
    a.admin.execute(update(Party).where(Party.id == a.pid).values(email=changed_email))
    a.admin.execute(update(PartyPerson).where(PartyPerson.party_id == a.pid).values(first_name="Current"))
    a.admin.execute(
        update(AcademyAccountProfile)
        .where(AcademyAccountProfile.party_id == a.pid)
        .values(prefs={"email_results": False})
    )
    a.admin.commit()
    root = a.db.get_transaction()
    recipient = completion._notification_recipient(a.db, tenant_id=a.tid, person_id=a.pid)
    assert recipient.id == a.pid and recipient.email == changed_email and recipient.first_name == "Current"
    assert recipient.prefs == {"email_results": False}
    assert not email.recipient_allows(recipient, "email_results")
    assert cached[0].email == a.email and cached[1].first_name == "Sign"
    assert a.db.get_transaction() is root


def test_wrong_tenant_and_unknown_party_refuse_recipient(current_account, tenant_b):
    a = current_account
    assert completion._notification_recipient(a.db, tenant_id=tenant_b.id, person_id=a.pid) is None
    assert completion._notification_recipient(a.db, tenant_id=a.tid, person_id=uuid4()) is None


@pytest.mark.parametrize("missing", ["profile", "subtype"])
def test_missing_canonical_recipient_part_refuses(current_account, missing):
    a = current_account
    # Deleting the Academy profile first satisfies its canonical subtype FK.
    a.admin.execute(delete(AcademyAccountProfile).where(AcademyAccountProfile.party_id == a.pid))
    if missing == "subtype":
        a.admin.execute(delete(PartyPerson).where(PartyPerson.party_id == a.pid))
    a.admin.commit()
    assert completion._notification_recipient(a.db, tenant_id=a.tid, person_id=a.pid) is None


@pytest.mark.parametrize("status,active", [("active", True), ("suspended", True), ("active", False)])
def test_delivery_read_preserves_status_behavior_without_authorizing_accounts(current_account, status, active):
    a = current_account
    a.admin.execute(update(Party).where(Party.id == a.pid).values(is_active=active))
    a.admin.execute(update(AcademyAccountProfile).where(AcademyAccountProfile.party_id == a.pid).values(status=status))
    a.admin.commit()
    assert completion._notification_recipient(a.db, tenant_id=a.tid, person_id=a.pid).id == a.pid


def test_certificate_recipient_uses_live_email_name_preferences_and_original_attribution(current_account, monkeypatch):
    a = current_account
    queued = []
    monkeypatch.setattr("app.services.email_outbox.enqueue_email", lambda db, **kw: queued.append(kw) or True)
    certificate = SimpleNamespace(id=uuid4(), serial="CANONICAL-1")
    course_id = uuid4()
    kwargs = dict(tenant_id=a.tid, person_id=a.pid, course_id=course_id, course_title="A & B", certificate=certificate)
    assert completion._email_certificate(a.db, **kwargs)
    assert queued[0]["recipient"] == a.email
    assert "Sign" in queued[0]["html_body"] and "A &amp; B" in queued[0]["html_body"]
    assert queued[0]["payload"] == {"person_id": str(a.pid), "course_id": str(course_id)}
    assert queued[0]["idempotency_key"] == f"certificate:{certificate.id}"
    a.admin.execute(
        update(AcademyAccountProfile)
        .where(AcademyAccountProfile.party_id == a.pid)
        .values(prefs={"email_results": False})
    )
    a.admin.commit()
    assert not completion._email_certificate(a.db, **kwargs)
    assert len(queued) == 1


def test_assessment_optional_intent_keeps_learning_person_and_tenant(current_account, monkeypatch):
    a = current_account
    activity = SimpleNamespace(id=uuid4(), type="mcq_test")
    score = SimpleNamespace(id=uuid4(), tenant_id=a.tid, person_id=a.pid)
    commands, deliveries = [], []
    monkeypatch.setattr(
        assessment.settings_store, "effective", lambda *args, **kw: {"learning_missing_question_weight": 1}
    )
    monkeypatch.setattr(
        assessment,
        "submit_mcq_activity",
        lambda db, command: commands.append(command) or SimpleNamespace(score=score, completion=None),
    )
    monkeypatch.setattr(email, "notify_score_if_first_pass", lambda db, **kw: deliveries.append(kw))
    assert assessment.submit_activity(a.db, tenant_id=a.tid, person_id=a.pid, activity=activity, answers={}) is score
    assert commands[0].tenant_id == a.tid and commands[0].person_id == a.pid
    assert deliveries[0]["person"].id == a.pid and deliveries[0]["person"].email == a.email
    assert deliveries[0]["score"] is score and deliveries[0]["activity"] is activity


def test_lab_capability_keeps_instance_identity_and_defers_recipient_reads(current_account, monkeypatch):
    a = current_account
    instance = SimpleNamespace(
        id=uuid4(),
        tenant_id=a.tid,
        person_id=a.pid,
        activity_id=uuid4(),
        status="active",
        error=None,
        seed={},
    )
    from app.services.lab_result_capability import LabGradeReceipt

    receipt = LabGradeReceipt(uuid4(), uuid4(), 1.0, 1.0, True)
    operation_id = uuid4()
    commands, order = [], []

    def checks(*args, **kwargs):
        order.append("checks")
        return {"score": 1, "max_score": 1, "per_check": []}

    def record(db, **command):
        assert db is a.db and not hasattr(instance, "last_active_at")
        commands.append(command)
        order.append("capability")
        return receipt

    def forbidden(*args, **kwargs):
        pytest.fail("worker grading must not read a recipient or deliver messages")

    monkeypatch.setattr(lab_lifecycle, "run_checks", checks)
    monkeypatch.setattr(lab_lifecycle, "record_claimed_lab_check", record)
    monkeypatch.setattr(completion, "_notification_recipient", forbidden)
    monkeypatch.setattr(completion, "deliver_completion_notifications", forbidden)
    monkeypatch.setattr(email, "notify_score_if_first_pass", forbidden)
    assert (
        lab_lifecycle.grade(
            a.db,
            instance,
            object(),
            SimpleNamespace(checks=[], limits={}),
            object(),
            operation_id=operation_id,
            claimed_by="identity-worker",
        )
        is receipt
    )
    assert commands == [
        {"tenant_id": a.tid, "operation_id": operation_id, "claimed_by": "identity-worker", "per_item": []}
    ]
    assert instance.person_id == a.pid and instance.tenant_id == a.tid
    assert instance.last_active_at is not None and order == ["checks", "capability"]


def test_restricted_app_role_recipient_read_needs_no_identity_update_grant(current_account):
    a = current_account
    assert a.db.scalar(text("SELECT current_user")) == "app_user"
    with write_free_identity_read(a):
        recipient = completion._notification_recipient(a.db, tenant_id=a.tid, person_id=a.pid)
        assert recipient.id == a.pid and recipient.email == a.email


def test_assessment_admission_refusal_precedes_optional_identity_read(current_account, monkeypatch):
    a = current_account
    monkeypatch.setattr(
        assessment.settings_store, "effective", lambda *args, **kw: {"learning_missing_question_weight": 1}
    )

    def refuse(db, command):
        raise assessment.AdmissionRefused("refused")

    def unexpected_read(*args, **kwargs):
        pytest.fail("refused submission must not read notification identity")

    monkeypatch.setattr(assessment, "submit_mcq_activity", refuse)
    monkeypatch.setattr(completion, "_notification_recipient", unexpected_read)
    with pytest.raises(ConflictError, match="refused"):
        assessment.submit_activity(
            a.db,
            tenant_id=a.tid,
            person_id=a.pid,
            activity=SimpleNamespace(id=uuid4(), type="mcq_test"),
            answers={},
        )
