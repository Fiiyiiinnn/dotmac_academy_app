"""Pure sender invariants; no database or network authority."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from app.models.erp_completion_checkpoint import ERPCompletionCheckpoint, ERPImportedCompletionKey
from app.services import erp_sync


@pytest.mark.parametrize(("code", "body", "expected"), [
    (200, {"status": "recorded"}, erp_sync.SYNCED),
    (200, {"status": "updated"}, erp_sync.SYNCED),
    (200, {"status": "duplicate"}, erp_sync.SYNCED),
    (200, {"status": "ignored"}, erp_sync.UNMATCHED),
    (422, {"detail": {"status": "unsupported"}}, erp_sync.UNMATCHED),
    (200, {"status": "unknown"}, erp_sync.FAILED),
    (503, {"status": "recorded"}, erp_sync.FAILED),
    (200, [], erp_sync.FAILED),
])
def test_explicit_receipt_required(code, body, expected):
    assert erp_sync._outcome(httpx.Response(code, json=body)) == expected


def test_unreadable_receipt_is_failure():
    assert erp_sync._outcome(httpx.Response(200, content=b"bad json")) == erp_sync.FAILED


def test_v2_payload_uses_stable_identity_and_snapshot():
    enrollment = SimpleNamespace(id=uuid4(), employee_ref="EMP-1")
    course = SimpleNamespace(source_ref="fiber@3", title="Fiber", version=3)
    completion = SimpleNamespace(
        id=uuid4(), status="in_progress", pct=0.12345,
        updated_at=datetime(2026, 10, 28, tzinfo=UTC), completed_at=None,
    )
    payload = erp_sync.build_payload(enrollment=enrollment, course=course, completion=completion)
    assert payload == {
        "version": 2, "event": "training_progress_updated", "employee_ref": "EMP-1",
        "academy_enrollment_ref": str(enrollment.id), "academy_course_ref": "fiber@3",
        "course_title": "Fiber", "course_version": 3, "progress_pct": 12.35,
        "status": "in_progress", "occurred_at": completion.updated_at.isoformat(),
    }
    completion.status = "completed"
    completion.completed_at = completion.updated_at
    payload = erp_sync.build_payload(enrollment=enrollment, course=course, completion=completion)
    assert payload["completed_on"] == "2026-10-28"
    assert payload["certificate_ref"] == str(completion.id)
    assert payload["passed"] is True


class _AbsentCheckpoint:
    def one_or_none(self):
        return None


class _LostImportedCheckpointSession:
    def __init__(self, tenant_id, completion_id):
        self.key = tenant_id, completion_id

    def scalars(self, statement):
        return _AbsentCheckpoint()

    def get(self, model, key):
        assert model is ERPImportedCompletionKey and key == self.key
        return ERPImportedCompletionKey(tenant_id=key[0], completion_id=key[1])

    def execute(self, statement):
        pytest.fail("lost imported checkpoint must refuse before lazy INSERT")


def test_missing_imported_checkpoint_refuses_before_insert_or_http(monkeypatch):
    tenant_id, completion_id = uuid4(), uuid4()
    completion = SimpleNamespace(tenant_id=tenant_id, id=completion_id)
    monkeypatch.setattr(erp_sync.settings, "erp_webhook_url", "https://erp.invalid")
    monkeypatch.setattr(erp_sync.httpx, "post", lambda *a, **k: pytest.fail("HTTP forbidden"))
    # A new session carries only persisted membership; no process-local receipt.
    for _ in range(2):
        db = _LostImportedCheckpointSession(tenant_id, completion_id)
        with pytest.raises(erp_sync.ERPCheckpointInvariantError, match="missing checkpoint"):
            erp_sync.push_completion(db, tenant_id=tenant_id, completion=completion)


def test_membership_fk_survives_checkpoint_deletion():
    targets = {fk.target_fullname for fk in ERPImportedCompletionKey.__table__.foreign_keys}
    assert targets == {"tenants.id", "mod_learning.course_completions.id", "mod_learning.course_completions.tenant_id"}
    assert set(ERPImportedCompletionKey.__table__.columns.keys()) == {"tenant_id", "completion_id"}
    assert ERPCompletionCheckpoint.__table__.c.erp_synced_pct.nullable


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def one_or_none(self):
        return self.value


class _SnapshotSession:
    def __init__(self, completion, course):
        self.reads = iter((completion, course))
        self.flushed = False

    def scalars(self, statement):
        assert statement.get_execution_options()["populate_existing"] is True
        return _ScalarResult(next(self.reads))

    def flush(self):
        self.flushed = True

    def commit(self):
        pytest.fail("sender must not commit")


@pytest.mark.parametrize("ack", ["recorded", "ignored", "unknown"])
def test_marker_tracks_sent_snapshot_and_failure_preserves_marker(monkeypatch, ack):
    tenant_id = uuid4()
    stale = SimpleNamespace(id=uuid4(), tenant_id=tenant_id, person_id=uuid4(), pct=0.1)
    current = SimpleNamespace(
        id=stale.id, tenant_id=tenant_id, person_id=stale.person_id, course_id=uuid4(),
        pct=0.4, status="in_progress", completed_at=None,
        updated_at=datetime(2026, 10, 28, tzinfo=UTC),
    )
    course = SimpleNamespace(source_ref="fiber@3", title="Fiber", version=3)
    enrollment = SimpleNamespace(id=uuid4(), employee_ref="EMP-1")
    checkpoint = SimpleNamespace(erp_synced_pct=0.2, erp_synced_at=None)
    monkeypatch.setattr(erp_sync.settings, "erp_webhook_url", "https://erp.invalid")
    monkeypatch.setattr(erp_sync.settings, "erp_webhook_secret", "test-secret")
    monkeypatch.setattr(erp_sync, "_locked_checkpoint", lambda *a, **k: checkpoint)
    monkeypatch.setattr(erp_sync, "_eligible_enrollments", lambda *a, **k: [enrollment])

    def post(url, *, content, headers, timeout):
        import hashlib
        import hmac
        import json

        assert json.loads(content)["progress_pct"] == 40
        assert headers["X-Webhook-Signature-256"] == "sha256=" + hmac.new(
            b"test-secret", content, hashlib.sha256,
        ).hexdigest()
        assert timeout == 15.0
        # A later Learning fact arriving during transport must remain eligible.
        current.pct = 0.75
        return httpx.Response(200, json={"status": ack})

    monkeypatch.setattr(erp_sync.httpx, "post", post)
    db = _SnapshotSession(current, course)
    outcome = erp_sync.push_completion(db, tenant_id=tenant_id, completion=stale)
    assert outcome == {"recorded": erp_sync.SYNCED, "ignored": erp_sync.UNMATCHED, "unknown": erp_sync.FAILED}[ack]
    assert checkpoint.erp_synced_pct == (0.4 if ack == "recorded" else 0.2)
    assert checkpoint.erp_synced_at is None
    assert current.pct == 0.75
    assert current.updated_at == datetime(2026, 10, 28, tzinfo=UTC)
    assert stale.pct == 0.1
    assert db.flushed is (ack == "recorded")
