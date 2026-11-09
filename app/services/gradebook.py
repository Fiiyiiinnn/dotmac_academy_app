"""Presentation containers over the public Learning weighted gradebook."""

# Ported at new-composition genesis (learning-facing wave).
from uuid import UUID

import dotmac_learning as learning
from sqlalchemy.orm import Session

from app.services.exceptions import NotFoundError


def cohort_gradebook(db: Session, *, tenant_id: UUID, cohort_id: UUID) -> dict:
    try:
        facts = learning.cohort_gradebook(db, tenant_id=tenant_id, cohort_id=cohort_id)
    except learning.ParticipationNotFound as exc:
        raise NotFoundError(str(exc)) from None
    return {
        "cohort": facts.cohort,
        "activities": list(facts.activities),
        "rows": [
            {
                "person": row.person,
                "email": row.email,
                "cells": [{"activity_id": cell.activity_id, "pct": cell.pct} for cell in row.cells],
                "final_pct": row.final_pct,
            }
            for row in facts.rows
        ],
    }
