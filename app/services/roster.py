"""Request-bound roster handoffs to Learning in the caller transaction.

The shared human boundary owns operation rollback and fresh authority. Learning
owns every enrollment write. State/remove derive fresh authority from the actual
Request through that private boundary; hosted qualification remains owed.
"""

# Ported at new-composition genesis (presentation-coupled wave).

from __future__ import annotations

from typing import cast
from uuid import UUID

import dotmac_learning as learning
from dotmac_kernel.models import Party, PartyPerson, PartyType
from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.identity_extensions import AcademyAccountProfile
from app.services import audit_context
from app.services.exceptions import BadRequestError, NotFoundError
from app.services.learning_policy import creation_defaults
from app.services.roster_policy import bulk_enrollment_policy

ROSTER_STATES = frozenset({"active", "waitlisted", "dropped"})


def _account_presence(tenant_id: UUID) -> learning.AccountPresenceSelection:
    """Live presence, including inactive accounts; never a cached ID population."""
    return learning.AccountPresenceSelection(
        tenant_id=tenant_id,
        person_ids=select(Party.id)
        .join(PartyPerson, PartyPerson.party_id == Party.id)
        .join(
            AcademyAccountProfile,
            (AcademyAccountProfile.party_id == Party.id) & (AcademyAccountProfile.tenant_id == Party.tenant_id),
        )
        .where(Party.tenant_id == tenant_id, Party.party_type == PartyType.person),
    )


# Facilitator authority is nomination-based (app/services/facilitator_nominations.py,
# #188/ADR 0010): there is deliberately no enrollment-rewriting facilitator
# writer here. Learning's AssignFacilitatorCommand is unused by Academy.


def bulk_enroll(
    db: Session,
    *,
    request: Request,
    tenant_id: UUID,
    cohort_id: UUID,
    emails,
    track_id: UUID | None = None,
) -> dict:
    """Keep normalized legacy per-email outcomes under one genuine operation."""
    with audit_context.bulk_enroll_web_roster(
        db, request=request, cohort_id=cohort_id, emails=emails, track_id=track_id
    ) as human:
        if human.tenant_id != tenant_id:
            raise HTTPException(status_code=403, detail="Forbidden")
        human.revalidate()
        if not learning.instructor_cohort_exists(db, tenant_id=tenant_id, cohort_id=cohort_id):
            raise NotFoundError("cohort not found for tenant")
        # One policy snapshot before any track/enrollment DML. Learning retains
        # its own cohort check, intentionally adding one scoped read.
        initial_state = bulk_enrollment_policy(db)
        pacing_default_open = creation_defaults(db).pacing_default_open
        actor_is_admin = human.actor_is_admin
        try:
            result = learning.bulk_enroll(
                db,
                learning.BulkEnrollCommand(
                    tenant_id=tenant_id,
                    cohort_id=cohort_id,
                    emails=human.resources.emails,
                    pacing_default_open=pacing_default_open,
                    track_id=track_id,
                ),
                authority=learning.RosterAuthority(tenant_id=tenant_id, actor_is_admin=actor_is_admin),
                accounts=_account_presence(tenant_id),
                initial_state=initial_state,
            )
        except learning.ParticipationNotFound as exc:
            raise NotFoundError(str(exc)) from None
        except learning.ParticipationBadRequest as exc:
            raise BadRequestError(str(exc)) from None
        except learning.ParticipationForbidden as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from None
        if actor_is_admin and not human.actor_is_admin:
            raise HTTPException(status_code=403, detail="Forbidden")
        return {
            key: list(getattr(result, key))
            for key in ("enrolled", "reactivated", "already_active", "not_found", "admin_required")
        }


def set_roster_state(
    db: Session,
    *,
    request: Request,
    tenant_id: UUID,
    cohort_id: UUID,
    person_id: UUID,
    state: str,
) -> learning.EnrollmentFact:
    """Bind the actual request; Learning retains state/role/error ordering.

    No eager target or state validation: the existing Learning command owns
    nonstudent authorization and invalid-state/missing-enrollment priority.
    """
    human = result = actor_is_admin = None
    try:
        with audit_context.change_web_roster_state(
            db, request=request, cohort_id=cohort_id, person_id=person_id, state=state
        ) as human:
            if human.tenant_id != tenant_id:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
            actor_is_admin = human.actor_is_admin
            try:
                result = learning.set_roster_state(
                    db,
                    learning.RosterStateCommand(
                        tenant_id=human.tenant_id,
                        cohort_id=human.resources.cohort_id,
                        person_id=human.resources.party_id,
                        state=human.resources.values[0],
                    ),
                    authority=learning.RosterAuthority(tenant_id=human.tenant_id, actor_is_admin=actor_is_admin),
                )
            except learning.ParticipationNotFound as exc:
                raise NotFoundError(str(exc)) from None
            except learning.ParticipationForbidden as exc:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from None
            if actor_is_admin and not human.actor_is_admin:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
            return result
    finally:
        db = cast(Session, None)
        request = cast(Request, None)
        tenant_id = cohort_id = person_id = cast(UUID, None)
        state = cast(str, None)
        human = result = actor_is_admin = None
