"""The worker's bounded, PII-free lab result capability."""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.metrics import LAB_RESULT_REFUSALS
from app.services.exceptions import ConflictError

logger = logging.getLogger(__name__)
MAX_TEXT_LENGTH = 4096
MAX_PAYLOAD_BYTES = 262144


@dataclass(frozen=True)
class LabGradeReceipt:
    submission_id: UUID
    score_id: UUID
    score: float
    max_score: float
    passed: bool


def _payload(per_item: list[dict]) -> str:
    if not isinstance(per_item, list):
        raise ConflictError("invalid lab check payload")
    items = []
    for original in per_item:
        if not isinstance(original, dict):
            raise ConflictError("invalid lab check payload")
        item = dict(original)
        for key in ("actual", "expected", "detail", "hint"):
            value = item.get(key)
            if key in ("actual", "expected") and isinstance(value, (int, float)):
                if isinstance(value, float) and not math.isfinite(value):
                    raise ConflictError("invalid lab check payload")
                value = str(value).lower() if isinstance(value, bool) else str(value)
                item[key] = value
            if value is not None and (not isinstance(value, str) or len(value) > MAX_TEXT_LENGTH):
                raise ConflictError("invalid lab check payload")
        items.append(item)
    try:
        serialized = json.dumps(items, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError):
        raise ConflictError("invalid lab check payload") from None
    if len(serialized.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ConflictError("invalid lab check payload")
    return serialized


def record_claimed_lab_check(
    db: Session, *, tenant_id: UUID, operation_id: UUID, claimed_by: str, per_item: list[dict]
) -> LabGradeReceipt:
    items = _payload(per_item)
    try:
        db.execute(
            text("SELECT pg_catalog.set_config('app.current_tenant', :tenant, true)"),
            {"tenant": str(tenant_id)},
        )
        row = (
            db.execute(
                text(
                    "SELECT * FROM public.record_claimed_lab_check("
                    "CAST(:operation AS pg_catalog.uuid), CAST(:claim AS pg_catalog.text), "
                    "CAST(:items AS pg_catalog.jsonb))"
                ),
                {"operation": str(operation_id), "claim": claimed_by, "items": items},
            )
            .mappings()
            .one()
        )
    except DBAPIError as exc:
        state = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
        safe_state = state if isinstance(state, str) and len(state) == 5 and state.isalnum() else "unknown"
        category = {"42501": "claim", "55000": "replay", "22004": "payload", "22023": "payload"}.get(
            safe_state, "database"
        )
        metric_state = (
            safe_state
            if safe_state in {"42501", "22004", "22023", "55000", "23505"}
            else ("unknown" if safe_state == "unknown" else "other")
        )
        LAB_RESULT_REFUSALS.labels(sqlstate=metric_state).inc()
        logger.warning(
            "lab_check_record_failure class=DBAPIError category=%s sqlstate=%s operation_id=%s tenant_id=%s",
            category,
            metric_state,
            operation_id,
            tenant_id,
        )
        raise ConflictError("lab check could not be recorded") from None
    return LabGradeReceipt(
        submission_id=row["submission_id"],
        score_id=row["score_id"],
        score=row["score"],
        max_score=row["max_score"],
        passed=row["passed"],
    )
