"""App-sync shadow sender for Academy -> ERP completions (pilot).

Mirrors each completion the legacy v2 webhook already delivered as a
`learning.completion.observation.v1` envelope to the destination-owned
receiver in dotmac_erp_v2. Shadow means shadow: this path never stamps
the checkpoint, never changes the legacy outcome, and never raises —
every result is an outcome string for logs and counts.

Event identity is the completion id, and the correlation id is derived
from it deterministically, so a retry produces the identical envelope
bytes (same idempotency key AND fingerprint at the destination)
instead of looking like a same-key conflict.

Progress updates are out of scope: they are state-derived, have no
durable event identity yet, and stay on the legacy webhook.
"""

from __future__ import annotations

import hashlib
import hmac
import logging

import httpx
from dotmac_app_sync import SyncEnvelope, encode_envelope
from dotmac_learning.models import Course, CourseCompletion
from dotmac_learning.participation_models import Enrollment

from app.config import settings
from app.services.erp_sync import build_payload

logger = logging.getLogger(__name__)

CAPABILITY_ID = "learning.completion.observation.v1"
SOURCE_APPLICATION = "dotmac_academy_app"

DISABLED = "disabled"
NOT_CONFIGURED = "not_configured"
SENT = "sent"
FAILED = "failed"


def build_completion_envelope(*, enrollment: Enrollment, course: Course, completion: CourseCompletion) -> SyncEnvelope:
    """The completion observation envelope for one delivered completion."""
    payload = build_payload(enrollment=enrollment, course=course, completion=completion)
    # The envelope's capability id + contract version replace the legacy
    # payload's version/event keys; the destination schema forbids them.
    payload.pop("version")
    payload.pop("event")
    occurred = completion.completed_at or completion.updated_at
    return SyncEnvelope(
        capability_id=CAPABILITY_ID,
        source_application=SOURCE_APPLICATION,
        source_event_id=str(completion.id),
        source_scope_kind="tenant",
        source_scope_ref=settings.erp_app_sync_source_scope_ref,
        subject_ref=str(enrollment.employee_ref),
        occurred_at=occurred.isoformat(),
        correlation_id=f"erp-sync:{completion.id}",
        payload=payload,
    )


def _sign(body: bytes) -> str:
    digest = hmac.new(settings.erp_webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def push_shadow_completion(*, enrollment: Enrollment, course: Course, completion: CourseCompletion) -> str:
    """Best-effort shadow send. Returns an outcome string; never raises."""
    if settings.erp_app_sync_mode != "shadow":
        return DISABLED
    if not settings.erp_app_sync_url or not settings.erp_webhook_secret:
        return NOT_CONFIGURED
    try:
        envelope = build_completion_envelope(enrollment=enrollment, course=course, completion=completion)
        body = encode_envelope(envelope)
        resp = httpx.post(
            settings.erp_app_sync_url,
            content=body,
            headers={"Content-Type": "application/json", "X-Webhook-Signature-256": _sign(body)},
            timeout=15.0,
        )
    except Exception as exc:  # shadow transport must never disturb the legacy outcome
        logger.warning("erp app-sync shadow send failed for completion %s: %s", completion.id, exc)
        return FAILED
    if resp.status_code // 100 != 2:
        logger.warning("erp app-sync shadow send rejected (%s) for completion %s", resp.status_code, completion.id)
        return FAILED
    return SENT


__all__ = [
    "CAPABILITY_ID",
    "DISABLED",
    "FAILED",
    "NOT_CONFIGURED",
    "SENT",
    "SOURCE_APPLICATION",
    "build_completion_envelope",
    "push_shadow_completion",
]
