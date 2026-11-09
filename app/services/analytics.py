"""Academy item-analysis template projection; Learning owns score selection."""

# Ported at new-composition genesis (learning-facing wave).

from __future__ import annotations

from uuid import UUID

import dotmac_learning as learning
from sqlalchemy.orm import Session


def item_analysis(db: Session, *, tenant_id: UUID, activity_id: UUID) -> list[dict]:
    return [
        {"id": item.id, "responses": item.responses, "correct": item.correct, "p_value": item.p_value}
        for item in learning.item_analysis(db, tenant_id=tenant_id, activity_id=activity_id)
    ]
