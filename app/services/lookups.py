"""Tenant-scoped instructor cohort facts from public Learning reads."""

# Ported at new-composition genesis (learning-facing wave).

from __future__ import annotations

from uuid import UUID

import dotmac_learning as learning
from sqlalchemy.orm import Session

from app.services.exceptions import NotFoundError


def cohort_or_404(db: Session, *, tenant_id: UUID, cohort_id: UUID) -> learning.ReportCohort:
    """Preserve cohort detail consumers without restoring an Academy query engine."""
    cohort = learning.instructor_cohort_by_id(db, tenant_id=tenant_id, cohort_id=cohort_id)
    if cohort is None:
        raise NotFoundError("cohort not found for tenant")
    return cohort
