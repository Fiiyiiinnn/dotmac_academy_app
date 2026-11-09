"""State-derived delivery of completed ERP applicant assessments."""

# Ported at new-composition genesis (ERP slice).

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.admissions import Applicant
from app.services.entrance_exam import LEVELS
from app.services.erp_applicant_assessments import SOURCE
from app.services.erp_sync import FAILED, SYNCED, UNMATCHED, _outcome, _sign
from app.services.exam_engine import INVALID_NEAR_CHANCE, INVALID_TOO_FAST

logger = logging.getLogger(__name__)

EVENT_TYPE = "entrance_assessment_completed"
CONTRACT_VERSION = 1
INVALID_REASONS = frozenset({INVALID_NEAR_CHANCE, INVALID_TOO_FAST})
MAX_PROFILE_PROPERTIES = 100


def _utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def build_payload(applicant: Applicant) -> dict:
    """Canonical v1 event derived only from a completed Applicant result."""
    if (
        applicant.external_ref is None
        or applicant.assessment_taken_at is None
        or applicant.assessment_score is None
        or applicant.assessment_level is None
        or applicant.assessment_profile is None
        or applicant.assessment_valid is None
        or applicant.assessment_bank_id is None
        or applicant.assessment_result_version < 1
    ):
        raise ValueError("applicant has no publishable completed assessment")
    score = float(applicant.assessment_score)
    if not 0.0 <= score <= 1.0:
        raise ValueError("assessment score is outside the canonical range")
    if applicant.assessment_level not in {name for name, _ in LEVELS}:
        raise ValueError("assessment level is not canonical")
    if applicant.assessment_invalid_reason is not None and applicant.assessment_invalid_reason not in INVALID_REASONS:
        raise ValueError("assessment invalid reason is not canonical")
    if applicant.assessment_valid and applicant.assessment_invalid_reason is not None:
        raise ValueError("valid assessment cannot have an invalid reason")
    if not applicant.assessment_valid and applicant.assessment_invalid_reason is None:
        raise ValueError("invalid assessment must have an invalid reason")
    if len(applicant.assessment_profile) > MAX_PROFILE_PROPERTIES:
        raise ValueError("assessment profile has too many properties")
    profile: dict[str, float] = {}
    for key, value in applicant.assessment_profile.items():
        if not isinstance(key, str) or not 1 <= len(key) <= 40:
            raise ValueError("assessment profile key is not canonical")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("assessment profile value is not numeric")
        fraction = float(value)
        if not 0.0 <= fraction <= 1.0:
            raise ValueError("assessment profile value is outside the canonical range")
        profile[key] = fraction

    return {
        "version": CONTRACT_VERSION,
        "event_type": EVENT_TYPE,
        "event_id": (f"entrance-assessment:{applicant.id}:v{applicant.assessment_result_version}"),
        "external_ref": applicant.external_ref,
        "assessment_bank_id": str(applicant.assessment_bank_id),
        "result_version": applicant.assessment_result_version,
        "completed_at": _utc(applicant.assessment_taken_at),
        "score": score,
        "level": applicant.assessment_level,
        "profile": profile,
        "is_valid": applicant.assessment_valid,
        "invalid_reason": applicant.assessment_invalid_reason,
        "time_exceeded": applicant.assessment_time_exceeded,
        # Academy has no result-expiration policy. The invitation deadline is
        # not result validity, so v1 represents that explicitly as null.
        "valid_until": None,
    }


def push_result(
    db: Session,
    *,
    applicant: Applicant,
    now: datetime | None = None,
) -> str:
    """Push one authoritative result; stamp only explicit ERP acceptance."""
    if not settings.erp_webhook_url:
        return FAILED
    try:
        payload = build_payload(applicant)
    except ValueError:
        return FAILED
    body = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    try:
        response = httpx.post(
            settings.erp_webhook_url,
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Webhook-Signature-256": _sign(body),
            },
            timeout=15.0,
        )
    except Exception as exc:
        logger.warning(
            "ERP assessment result delivery failed applicant=%s error=%s",
            applicant.id,
            type(exc).__name__,
        )
        return FAILED

    outcome = _outcome(response)
    if outcome != SYNCED:
        logger.warning(
            "ERP assessment result not accepted applicant=%s status=%s outcome=%s",
            applicant.id,
            response.status_code,
            outcome,
        )
        return outcome

    applicant.assessment_erp_synced_at = now or datetime.now(UTC)
    db.flush()
    return SYNCED


def sync_pending(
    db: Session,
    *,
    tenant_id: UUID,
    now: datetime | None = None,
) -> dict[str, int]:
    counts = {SYNCED: 0, UNMATCHED: 0, FAILED: 0}
    if not settings.erp_webhook_url:
        return counts
    applicants = db.scalars(
        select(Applicant)
        .where(Applicant.tenant_id == tenant_id)
        .where(Applicant.source == SOURCE)
        .where(Applicant.external_ref.is_not(None))
        .where(Applicant.assessment_taken_at.is_not(None))
        .where(Applicant.assessment_erp_synced_at.is_(None))
        .order_by(Applicant.assessment_taken_at, Applicant.id)
    ).all()
    for applicant in applicants:
        counts[push_result(db, applicant=applicant, now=now)] += 1
    return counts
