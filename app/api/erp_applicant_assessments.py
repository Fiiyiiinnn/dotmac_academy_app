"""Signed ERP -> Academy applicant-assessment registration endpoint."""

# Ported at new-composition genesis (API routers wave).

from __future__ import annotations

import logging
from datetime import datetime
from typing import Literal
from uuid import UUID

from dotmac_kernel.models import Tenant
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, Field, ValidationError, field_validator
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_tenant
from app.services import erp_applicant_assessments as registration
from app.services.erp_integration_security import IntegrationAuthError, verify_request

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/integrations/erp",
    tags=["erp-integration"],
    dependencies=[Depends(require_tenant)],
)


class ApplicantAssessmentRegistration(BaseModel):
    external_ref: str = Field(min_length=1, max_length=64)
    email: EmailStr
    first_name: str = Field(min_length=1, max_length=80)
    last_name: str = Field(min_length=1, max_length=80)
    assessment_bank_id: UUID | None = None
    return_url: str = Field(min_length=1, max_length=2048)

    model_config = {"extra": "forbid"}

    @field_validator("external_ref")
    @classmethod
    def valid_external_ref(cls, value: str) -> str:
        if not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("external_ref contains invalid characters")
        return value

    @field_validator("first_name", "last_name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must not be blank")
        return value


class ApplicantAssessmentRegistrationResponse(BaseModel):
    assessment_url: str
    expires_at: datetime
    state: Literal["not_started", "in_progress", "completed"]


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )


@router.post(
    "/applicant-assessments",
    response_model=ApplicantAssessmentRegistrationResponse,
    responses={
        400: {"description": "Malformed JSON"},
        401: {"description": "Missing, stale, or invalid HMAC authentication"},
        409: {"description": "Conflicting or expired registration"},
        422: {"description": "Invalid request, bank, or return URL"},
        503: {"description": "Integration not configured"},
    },
)
async def register_applicant_assessment(
    request: Request,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
):
    body = await request.body()
    try:
        verify_request(
            body=body,
            timestamp=request.headers.get("X-Webhook-Timestamp"),
            signature=request.headers.get("X-Webhook-Signature-256"),
        )
    except IntegrationAuthError as exc:
        if exc.code == "integration_disabled":
            return _error(503, exc.code, "Applicant assessment registration is unavailable")
        if exc.code == "request_too_large":
            return _error(413, exc.code, "Request body is too large")
        return _error(401, exc.code, "Request authentication failed")

    try:
        payload = ApplicantAssessmentRegistration.model_validate_json(body)
    except ValidationError as exc:
        malformed = any(error["type"] == "json_invalid" for error in exc.errors())
        return _error(
            400 if malformed else 422,
            "malformed_json" if malformed else "invalid_request",
            "Request body is not valid JSON" if malformed else "Request fields are invalid",
        )

    try:
        result = registration.register(
            db,
            tenant_id=tenant.id,
            external_ref=payload.external_ref,
            email=str(payload.email),
            first_name=payload.first_name,
            last_name=payload.last_name,
            assessment_bank_id=payload.assessment_bank_id,
            return_url=payload.return_url,
        )
    except registration.RegistrationError as exc:
        return _error(exc.status_code, exc.code, exc.message)
    except Exception:
        # Never log the signed body, PII fields, or generated assessment token.
        logger.exception("ERP applicant assessment registration failed")
        return _error(500, "internal_error", "Could not register applicant assessment")

    return ApplicantAssessmentRegistrationResponse(
        assessment_url=result.assessment_url,
        expires_at=result.expires_at,
        state=result.state,
    )
