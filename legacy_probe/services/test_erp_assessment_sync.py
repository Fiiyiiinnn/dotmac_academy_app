"""Academy -> ERP entrance_assessment_completed delivery contract."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.config import settings
from app.models.admissions import Applicant
from app.models.assessment import QuestionBank
from app.models.course import Course
from app.services import erp_assessment_sync, erp_sync


class _Response:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = {"status": "recorded"} if body is None else body

    def json(self):
        return self._body


def _applicant(db, tenant):
    suffix = uuid4().hex[:8]
    course = Course(
        tenant_id=tenant.id,
        slug=f"erp-result-{suffix}",
        title="ERP result test",
        discipline="testing",
        source_ref=f"erp-result-{suffix}",
        status="published", listed=False,
    )
    db.add(course)
    db.flush()
    bank = QuestionBank(
        tenant_id=tenant.id,
        course_id=course.id,
        kind="final",
        version=1,
    )
    db.add(bank)
    db.flush()
    applicant = Applicant(
        tenant_id=tenant.id,
        email=f"{uuid4().hex[:8]}@example.com",
        first_name="Ada",
        last_name="Lovelace",
        source="erp_live",
        external_ref=" ERP-opaque:42 ",
        assessment_bank_id=bank.id,
        assessment_score=0.82,
        assessment_level="advanced",
        assessment_profile={"aptitude": 0.75, "safety": 1.0},
        assessment_valid=True,
        assessment_time_exceeded=False,
        assessment_taken_at=datetime(2026, 8, 7, 12, 30, 15, 123456, tzinfo=UTC),
        assessment_result_version=1,
    )
    db.add(applicant)
    db.flush()
    return applicant


def _configure(monkeypatch):
    monkeypatch.setattr(settings, "erp_webhook_url", "https://erp.example/dotmac-academy/webhook")
    monkeypatch.setattr(settings, "erp_webhook_secret", "test-outbound-secret")


def test_payload_is_exact_and_preserves_external_ref(admin_session, tenant_a):
    applicant = _applicant(admin_session, tenant_a)
    payload = erp_assessment_sync.build_payload(applicant)
    assert payload == {
        "version": 1,
        "event_type": "entrance_assessment_completed",
        "event_id": f"entrance-assessment:{applicant.id}:v1",
        "external_ref": " ERP-opaque:42 ",
        "assessment_bank_id": str(applicant.assessment_bank_id),
        "result_version": 1,
        "completed_at": "2026-08-07T12:30:15.123456Z",
        "score": 0.82,
        "level": "advanced",
        "profile": {"aptitude": 0.75, "safety": 1.0},
        "is_valid": True,
        "invalid_reason": None,
        "time_exceeded": False,
        "valid_until": None,
    }


def test_delivery_uses_existing_body_hmac_and_stamps_success(
    admin_session, tenant_a, monkeypatch
):
    _configure(monkeypatch)
    applicant = _applicant(admin_session, tenant_a)
    captured = {}

    def post(url, content=None, headers=None, timeout=None):
        captured.update(url=url, content=content, headers=headers, timeout=timeout)
        return _Response()

    monkeypatch.setattr(erp_assessment_sync.httpx, "post", post)
    outcome = erp_assessment_sync.push_result(admin_session, applicant=applicant)

    assert outcome == erp_sync.SYNCED
    assert applicant.assessment_erp_synced_at is not None
    expected = "sha256=" + hmac.new(
        b"test-outbound-secret", captured["content"], hashlib.sha256
    ).hexdigest()
    assert captured["headers"] == {
        "Content-Type": "application/json",
        "X-Webhook-Signature-256": expected,
    }
    assert "X-Webhook-Timestamp" not in captured["headers"]
    assert captured["timeout"] == 15.0
    assert json.loads(captured["content"])["external_ref"] == " ERP-opaque:42 "


def test_failed_delivery_remains_pending_and_retries(
    admin_session, tenant_a, monkeypatch
):
    _configure(monkeypatch)
    applicant = _applicant(admin_session, tenant_a)
    replies = iter([_Response(503, {"detail": "down"}), _Response(200, {"status": "updated"})])
    monkeypatch.setattr(
        erp_assessment_sync.httpx,
        "post",
        lambda *args, **kwargs: next(replies),
    )

    first = erp_assessment_sync.sync_pending(admin_session, tenant_id=tenant_a.id)
    assert first[erp_sync.FAILED] == 1
    assert applicant.assessment_erp_synced_at is None

    second = erp_assessment_sync.sync_pending(admin_session, tenant_id=tenant_a.id)
    assert second[erp_sync.SYNCED] == 1
    assert applicant.assessment_erp_synced_at is not None

    third = erp_assessment_sync.sync_pending(admin_session, tenant_id=tenant_a.id)
    assert third == {erp_sync.SYNCED: 0, erp_sync.UNMATCHED: 0, erp_sync.FAILED: 0}


def test_profile_contract_rejects_non_numeric_or_oversized_data(admin_session, tenant_a):
    applicant = _applicant(admin_session, tenant_a)
    applicant.assessment_profile = {"aptitude": "0.75"}
    with pytest.raises(ValueError):
        erp_assessment_sync.build_payload(applicant)

    applicant.assessment_profile = {f"category-{index}": 0.5 for index in range(101)}
    with pytest.raises(ValueError):
        erp_assessment_sync.build_payload(applicant)


def test_newer_result_version_generates_new_event_id(
    admin_session, tenant_a, monkeypatch
):
    _configure(monkeypatch)
    applicant = _applicant(admin_session, tenant_a)
    first = erp_assessment_sync.build_payload(applicant)
    applicant.assessment_score = 0.79
    applicant.assessment_result_version += 1
    applicant.assessment_erp_synced_at = None
    second = erp_assessment_sync.build_payload(applicant)

    assert first["event_id"] != second["event_id"]
    assert second["result_version"] == 2
    assert second["score"] == 0.79
