"""Academy app-sync shadow sender (pilot, completions only).

The shadow path mirrors a legacy webhook send that already succeeded:
it must be wire-valid for the ERP destination contract, byte-stable
across retries (same event -> same idempotency fingerprint), signed
with the existing webhook HMAC, and incapable of changing the legacy
outcome — every shadow failure is an outcome string, never a raise.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from dotmac_app_sync import encode_envelope, fingerprint_for, idempotency_key_for

from app.config import settings
from app.services import erp_app_sync


def _inputs():
    completion = SimpleNamespace(
        id=uuid.uuid4(),
        status="completed",
        pct=1.0,
        completed_at=datetime(2026, 11, 8, 10, 0, tzinfo=UTC),
        updated_at=datetime(2026, 11, 8, 10, 0, tzinfo=UTC),
    )
    enrollment = SimpleNamespace(id=uuid.uuid4(), employee_ref="E001")
    course = SimpleNamespace(source_ref="course-1", title="Safety Basics", version=2)
    return enrollment, course, completion


def test_envelope_identity_is_stable_for_retry_dedupe():
    enrollment, course, completion = _inputs()
    first = erp_app_sync.build_completion_envelope(enrollment=enrollment, course=course, completion=completion)
    second = erp_app_sync.build_completion_envelope(enrollment=enrollment, course=course, completion=completion)
    assert first.source_event_id == str(completion.id)
    assert first.capability_id == "learning.completion.observation.v1"
    assert first.source_application == "dotmac_academy_app"
    assert idempotency_key_for(first) == idempotency_key_for(second)
    assert fingerprint_for(first) == fingerprint_for(second), "a retry must not look like a conflict"
    wire = json.loads(encode_envelope(first))
    assert wire["payload"]["employee_ref"] == "E001"
    assert wire["payload"]["certificate_ref"] == str(completion.id)
    assert "version" not in wire["payload"] and "event" not in wire["payload"]


def _arm_shadow(monkeypatch, *, mode="shadow", url="https://erp.example/integration/academy-sync/completions"):
    monkeypatch.setattr(settings, "erp_app_sync_mode", mode)
    monkeypatch.setattr(settings, "erp_app_sync_url", url)
    monkeypatch.setattr(settings, "erp_webhook_secret", "unit-secret")


def test_shadow_disabled_by_default(monkeypatch):
    _arm_shadow(monkeypatch, mode="off")
    monkeypatch.setattr(erp_app_sync.httpx, "post", lambda *a, **k: pytest.fail("shadow must not send while disabled"))
    enrollment, course, completion = _inputs()
    assert (
        erp_app_sync.push_shadow_completion(enrollment=enrollment, course=course, completion=completion) == "disabled"
    )


def test_shadow_sends_signed_envelope_and_never_raises(monkeypatch):
    _arm_shadow(monkeypatch)
    seen = {}

    def fake_post(url, *, content, headers, timeout):
        seen.update(url=url, content=content, headers=headers)
        return httpx.Response(200, json={"status": "accepted", "destination_ref": "x:1", "error_code": None})

    monkeypatch.setattr(erp_app_sync.httpx, "post", fake_post)
    enrollment, course, completion = _inputs()
    outcome = erp_app_sync.push_shadow_completion(enrollment=enrollment, course=course, completion=completion)
    assert outcome == "sent"
    expected = "sha256=" + hmac.new(b"unit-secret", seen["content"], hashlib.sha256).hexdigest()
    assert seen["headers"]["X-Webhook-Signature-256"] == expected

    def boom(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(erp_app_sync.httpx, "post", boom)
    assert erp_app_sync.push_shadow_completion(enrollment=enrollment, course=course, completion=completion) == "failed"


def test_shadow_requires_url(monkeypatch):
    _arm_shadow(monkeypatch, url="")
    enrollment, course, completion = _inputs()
    assert (
        erp_app_sync.push_shadow_completion(enrollment=enrollment, course=course, completion=completion)
        == "not_configured"
    )
