"""Authenticated Academy administrator admissions API.

Public intake has one canonical adapter: the complete ``/apply`` web flow.
"""

# Ported at new-composition genesis (API routers wave).

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from dotmac_kernel.models import Tenant
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_role, require_tenant
from app.models.admissions import Applicant
from app.models.onboarding import OnboardingTask
from app.services import admissions as admissions_service
from app.services import onboarding as onboarding_service
from app.services.identity import AcademyCurrentAccount

router = APIRouter(
    prefix="/admissions",
    tags=["admissions"],
    dependencies=[Depends(require_tenant)],
)


class ApplicantRead(BaseModel):
    id: UUID
    email: EmailStr
    first_name: str
    last_name: str
    phone: str | None
    program: str | None
    status: str
    source: str
    applied_on: date
    person_id: UUID | None = None
    # Entrance-assessment result (the candidate competency profile).
    cohort_id: UUID | None = None
    track_id: UUID | None = None
    assessment_score: float | None = None
    assessment_level: str | None = None
    assessment_profile: dict | None = None
    assessment_taken_at: datetime | None = None
    assessment_time_exceeded: bool = False
    # Validity: False = the sitting carries NO SIGNAL (near-chance, or too fast to
    # have engaged). Do not read such a score as a weak candidate — it is an
    # absence of data, and it is excluded from score-ranked listings.
    assessment_valid: bool | None = None
    assessment_invalid_reason: str | None = None
    assessment_deadline: datetime | None = None
    invite_sent_at: datetime | None = None
    assessment_reset_count: int = 0

    # --- the evaluable application profile ---
    date_of_birth: date | None = None
    state: str | None = None
    city: str | None = None
    highest_qualification: str | None = None
    field_of_study: str | None = None
    years_experience: int | None = None
    current_role: str | None = None
    has_device: bool | None = None
    has_internet: bool | None = None
    can_work_at_height: bool | None = None
    available_from: date | None = None
    heard_from: str | None = None
    cv_url: str | None = None
    # Can this candidate actually be evaluated yet?
    profile_complete: bool = False
    missing_profile_fields: list[str] = []

    model_config = {"from_attributes": True}


class ApplicantTransition(BaseModel):
    to_status: str = Field(min_length=1, max_length=20)
    notes: str | None = Field(default=None, max_length=1000)


class ApplicantEnroll(BaseModel):
    cohort_id: UUID


class OnboardingTaskRead(BaseModel):
    id: UUID
    key: str
    label: str
    order_index: int
    status: str
    completed_at: datetime | None = None
    model_config = {"from_attributes": True}


class OnboardingTaskUpdate(BaseModel):
    status: str = Field(min_length=1, max_length=10)


@router.get("", response_model=list[ApplicantRead])
def list_applicants(
    status: str | None = Query(default=None),
    cohort_id: UUID | None = Query(default=None),
    rank: bool = Query(default=False, description="Order by entrance-assessment score, best first"),
    db: Session = Depends(get_db),
    _: AcademyCurrentAccount = Depends(require_role("admin")),
) -> list[Applicant]:
    return admissions_service.list_applicants(db, status=status, cohort_id=cohort_id, rank_by_score=rank)


@router.get("/{applicant_id}", response_model=ApplicantRead)
def get_applicant(
    applicant_id: UUID,
    db: Session = Depends(get_db),
    _: AcademyCurrentAccount = Depends(require_role("admin")),
) -> object:
    return admissions_service.get_applicant(db, applicant_id=applicant_id)


@router.post("/{applicant_id}/transition", response_model=ApplicantRead)
def transition_applicant(
    applicant_id: UUID,
    payload: ApplicantTransition,
    request: Request,
    db: Session = Depends(get_db),
    actor: AcademyCurrentAccount = Depends(require_role("admin")),
) -> object:
    """Advance an applicant through the pipeline (screen / accept / reject / etc.).

    Entering ``onboarding`` also mints the applicant's self-serve portal token
    and emails the onboarding invitation — from there the applicant can finish
    enrolment without an admin.
    """
    return admissions_service.transition_api_applicant(
        db,
        request=request,
        applicant_id=applicant_id,
        to_status=payload.to_status,
        notes=payload.notes,
    )


@router.post("/{applicant_id}/enroll", response_model=ApplicantRead)
def enroll_applicant(
    applicant_id: UUID,
    payload: ApplicantEnroll,
    request: Request,
    db: Session = Depends(get_db),
    actor: AcademyCurrentAccount = Depends(require_role("admin")),
) -> object:
    """Enrol an onboarding applicant: create/reuse a Person + Enrollment."""
    return admissions_service.enroll_applicant(
        db,
        applicant_id=applicant_id,
        cohort_id=payload.cohort_id,
        request=request,
    )


@router.get("/{applicant_id}/onboarding", response_model=list[OnboardingTaskRead])
def list_onboarding_tasks(
    applicant_id: UUID,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
    _: AcademyCurrentAccount = Depends(require_role("admin")),
) -> list[OnboardingTask]:
    """The applicant's onboarding checklist, ordered."""
    return onboarding_service.list_tasks(db, tenant_id=tenant.id, applicant_id=applicant_id)


@router.post("/onboarding-tasks/{task_id}", response_model=OnboardingTaskRead)
def update_onboarding_task(
    task_id: UUID,
    payload: OnboardingTaskUpdate,
    db: Session = Depends(get_db),
    _: AcademyCurrentAccount = Depends(require_role("admin")),
) -> object:
    """Mark an onboarding task done (or back to pending)."""
    return onboarding_service.set_task_status(db, task_id=task_id, status=payload.status)
