"""Academy legacy dashboard projection over the public Learning read owner."""

# Ported at new-composition genesis (learning-facing wave).

from __future__ import annotations

from datetime import datetime
from typing import cast
from uuid import UUID

import dotmac_learning as learning
from sqlalchemy.orm import Session

from app.services.exceptions import NotFoundError
from app.services.settings_store import effective


def cohort_overview(
    db: Session,
    *,
    tenant_id: UUID,
    cohort_id: UUID,
    now: datetime | None = None,
    at_risk_pct: float | None = None,
    stale_days: int | None = None,
) -> dict:
    """Retain template keys and explicit overrides, composing defaults from Settings."""
    cfg = effective(db, require_store=True)
    try:
        fact = learning.legacy_cohort_overview(
            db,
            tenant_id=tenant_id,
            cohort_id=cohort_id,
            now=now,
            at_risk_pct=(
                cast(float, cfg["learning_instructor_at_risk_fraction"]) if at_risk_pct is None else at_risk_pct
            ),
            stale_days=(cast(int, cfg["learning_instructor_stale_days"]) if stale_days is None else stale_days),
        )
    except learning.ParticipationNotFound as exc:
        raise NotFoundError(str(exc)) from exc
    return {
        "cohort": fact.cohort,
        "rows": [
            {
                "person_id": row.person.id,
                "name": f"{row.person.first_name} {row.person.last_name}".strip(),
                "email": row.person.email,
                "completion_pct": row.completion_pct,
                "last_activity_at": row.last_activity_at,
                "at_risk": row.at_risk,
            }
            for row in fact.rows
        ],
    }
