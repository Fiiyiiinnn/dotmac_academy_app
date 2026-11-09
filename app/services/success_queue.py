"""Academy Success Queue adapters over the public Learning owner.

WEB mutations enter the genuine private request boundary before Learning executes.
"""

# Ported at new-composition genesis (presentation-coupled wave).

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import dotmac_learning as learning
from dotmac_kernel.models import Party, PartyPerson, PartyType
from dotmac_learning.success_queue import severity_order as severity_order
from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import require_tenant
from app.models.identity_extensions import AcademyAccountProfile
from app.services import audit_context
from app.services.exceptions import BadRequestError
from app.services.learning_policy import success_terms

SEGMENT_KEYS = learning.SEGMENT_KEYS
SEGMENTS_DEFERRED = learning.SEGMENTS_DEFERRED
# severity_order lives in the success_queue submodule, not the root exports.


def _accounts(tenant_id: UUID) -> learning.ActiveAccountSelection:
    # Learning composes this tenant-bound canonical active-account relation;
    # it remains the sole owner of success rules and queue persistence.
    return learning.ActiveAccountSelection(
        tenant_id,
        select(Party.id)
        .join(PartyPerson, PartyPerson.party_id == Party.id)
        .join(
            AcademyAccountProfile,
            (AcademyAccountProfile.party_id == Party.id) & (AcademyAccountProfile.tenant_id == Party.tenant_id),
        )
        .where(
            Party.tenant_id == tenant_id,
            Party.party_type == PartyType.person,
            Party.is_active.is_(True),
            AcademyAccountProfile.status == "active",
        ),
    )


def active_account_selection(tenant_id: UUID) -> learning.ActiveAccountSelection:
    """Public product account-status observation shared by Learning read adapters."""
    return _accounts(tenant_id)


def sweep(db: Session, *, tenant_id: UUID, now: datetime | None = None) -> dict:
    result = learning.sweep_success_queue(
        db, tenant_id=tenant_id, terms=success_terms(db), accounts=_accounts(tenant_id), now=now
    )
    return {"opened": result.opened, "refreshed": result.refreshed, "auto_resolved": result.auto_resolved}


def transition(db: Session, *, request: Request, tenant_id: UUID, entry_id: UUID, action: str) -> learning.QueueFact:
    if require_tenant(request).id != tenant_id:
        raise audit_context.HumanOperationError("success command tenant scope differs")
    with audit_context.transition_web_success_entry(db, request=request, entry_id=entry_id, action=action) as context:
        return context.transition_success_entry().entry


def list_entries(
    db: Session,
    *,
    tenant_id: UUID,
    status: str | None = None,
    severity: str | None = None,
    cohort_id: UUID | None = None,
) -> list[dict]:
    return [
        {"entry": row.entry, "person": row.person, "cohort_name": row.cohort_name}
        for row in learning.list_success_entries(
            db, tenant_id=tenant_id, status=status, severity=severity, cohort_id=cohort_id
        )
    ]


def segment_members(
    db: Session, *, tenant_id: UUID, cohort_id: UUID, segment: str, now: datetime | None = None
) -> list[learning.SuccessPersonFact]:
    try:
        return list(
            learning.segment_members(
                db,
                tenant_id=tenant_id,
                cohort_id=cohort_id,
                segment=segment,
                terms=success_terms(db),
                accounts=_accounts(tenant_id),
                now=now,
            )
        )
    except learning.SuccessBadRequest as exc:
        raise BadRequestError(str(exc)) from exc


def message_segment(
    db: Session,
    *,
    request: Request,
    tenant_id: UUID,
    cohort_id: UUID,
    segment: str,
    subject: str,
    body: str,
    now: datetime | None = None,
) -> dict:
    if require_tenant(request).id != tenant_id:
        raise audit_context.HumanOperationError("success command tenant scope differs")
    with audit_context.message_web_success_segment(
        db, request=request, cohort_id=cohort_id, segment=segment, subject=subject, body=body, now=now
    ) as context:
        result = context.message_success_segment().result
        return {"recipients": result.recipients, "action_id": result.action_id}
