"""Public Learning attempt entitlements and trusted grant-command adapter."""

# Ported at new-composition genesis (learning-facing wave 2).

from __future__ import annotations

from typing import cast
from uuid import UUID

import dotmac_learning as learning
from dotmac_learning import (
    ActivityTerms,
    BoundGrantReader,
    Entitlement,
    entitlement,
    entitlement_with_grants,
    granted_by_activity,
    granted_for,
)
from sqlalchemy.orm import Session

from app.services import settings_store

__all__ = [
    "ActivityTerms",
    "BoundGrantReader",
    "Entitlement",
    "entitlement",
    "entitlement_with_grants",
    "for_learner",
    "grant_extra_attempt",
    "granted_by_activity",
    "granted_for",
]


def for_learner(
    db: Session,
    *,
    tenant_id: UUID,
    person_id: UUID,
    activity: learning.ActivityFact,
    used: int,
) -> Entitlement:
    """Read this learner's entitlement through the public owner."""
    return learning.for_learner(db, tenant_id=tenant_id, person_id=person_id, activity=activity, used=used)


def grant_extra_attempt(
    db: Session,
    *,
    tenant_id: UUID,
    person_id: UUID,
    activity_id: UUID,
    reason: str,
    granted_by: UUID | None = None,
    extra_attempts: int | None = None,
    audit_context: learning.AttemptAuditContext,
) -> learning.AttemptGrantFact:
    """Append a grant with explicit server-owned audit attribution."""
    return learning.grant_extra_attempt(
        db,
        learning.GrantExtraAttemptCommand(
            tenant_id=tenant_id,
            person_id=person_id,
            activity_id=activity_id,
            reason=reason,
            granted_by=granted_by,
            extra_attempts=(
                cast(int, settings_store.effective(db, require_store=True)["learning_default_extra_attempts"])
                if extra_attempts is None
                else extra_attempts
            ),
        ),
        audit_context=audit_context,
    )
