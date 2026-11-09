"""Academy dictionary projections of public Learning report DTOs."""

# Ported at new-composition genesis (learning-facing wave).
from __future__ import annotations

from uuid import UUID

import dotmac_learning as learning
from sqlalchemy.orm import Session

from app.services.exceptions import NotFoundError


def cohort_matrix(db: Session, *, tenant_id: UUID, cohort_id: UUID) -> dict:
    try:
        fact = learning.cohort_matrix(db, tenant_id=tenant_id, cohort_id=cohort_id)
    except learning.ParticipationNotFound as exc:
        raise NotFoundError(str(exc)) from exc
    return {
        "cohort": fact.cohort,
        "activities": list(fact.activities),
        "rows": [
            {
                "person_id": r.person_id,
                "name": f"{r.first_name} {r.last_name}".strip(),
                "email": r.email,
                "cells": {c.activity_id: c.score for c in r.cells},
                "completion": r.completion,
            }
            for r in fact.rows
        ],
    }


def student_transcript(db: Session, *, tenant_id: UUID, person_id: UUID) -> dict:
    try:
        fact = learning.student_transcript(db, tenant_id=tenant_id, person_id=person_id)
    except learning.ReportNotFound as exc:
        raise NotFoundError(str(exc)) from exc
    return {
        "person": fact.person,
        "rows": [
            {
                "activity": r.activity,
                "score": r.score,
                "fraction": r.fraction,
                "passed": r.passed,
                "graded_at": r.graded_at,
                "source": r.source,
            }
            for r in fact.rows
        ],
    }
