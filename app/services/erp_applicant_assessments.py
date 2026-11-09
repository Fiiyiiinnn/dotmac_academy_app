"""ERP-owned job application -> Academy entrance-assessment registration."""

# Ported at new-composition genesis (ERP slice).

from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import quote, urlsplit
from uuid import UUID

from dotmac_learning.curriculum_models import Question, QuestionBank
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.admissions import Applicant
from app.services import admissions
from app.services.entrance_exam import academy_default_bank_id
from app.services.exceptions import ConflictError
from app.services.identity import normalize_email
from app.services.security import hash_token

SOURCE = "erp_live"
RegistrationState = Literal["not_started", "in_progress", "completed"]
AssessmentState = Literal["not_started", "in_progress", "completed", "expired"]
DEFAULT_DEADLINE_DAYS = 7


class RegistrationError(ValueError):
    def __init__(self, code: str, message: str, *, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class RegistrationResult:
    assessment_url: str
    expires_at: datetime
    state: RegistrationState


def _origin(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return None
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    return f"https://{host}" + (f":{port}" if port is not None and port != 443 else "")


def validate_return_url(value: str) -> str:
    if len(value) > 2048:
        raise RegistrationError("invalid_return_url", "return_url is not allowed")
    parsed = urlsplit(value)
    origin = _origin(value)
    if origin is None or parsed.fragment:
        raise RegistrationError("invalid_return_url", "return_url is not allowed")
    allowed = {
        candidate
        for raw in settings.erp_allowed_return_origins.split(",")
        if (candidate := _origin(raw.strip())) is not None
    }
    if origin not in allowed:
        raise RegistrationError("invalid_return_url", "return_url is not allowed")
    return value


def _public_base_url() -> str:
    value = settings.academy_public_base_url.rstrip("/")
    parsed = urlsplit(value)
    if _origin(value) is None or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise RegistrationError(
            "integration_not_configured",
            "Applicant assessment registration is unavailable",
            status_code=503,
        )
    return value


def _resolve_bank(db: Session, *, tenant_id: UUID, requested: UUID | None) -> QuestionBank:
    bank_id = requested
    if bank_id is None:
        bank_id = academy_default_bank_id(db, tenant_id=tenant_id)
        if bank_id is None:
            raise RegistrationError(
                "assessment_bank_required",
                "No default entrance assessment is configured",
            )
    bank = db.scalars(
        select(QuestionBank).where(QuestionBank.tenant_id == tenant_id).where(QuestionBank.id == bank_id)
    ).first()
    if bank is None:
        raise RegistrationError("unknown_assessment_bank", "Assessment bank is not available")
    question_count = db.scalar(
        select(func.count())
        .select_from(Question)
        .where(Question.tenant_id == tenant_id)
        .where(Question.bank_id == bank.id)
    )
    if not question_count:
        raise RegistrationError("unknown_assessment_bank", "Assessment bank is not available")
    return bank


def _integration_token(applicant: Applicant) -> str:
    secret = settings.erp_assessment_token_secret
    if not secret:
        raise RegistrationError(
            "integration_not_configured",
            "Applicant assessment registration is unavailable",
            status_code=503,
        )
    preimage = (
        f"erp-applicant-assessment:v1:{applicant.tenant_id}:{applicant.id}:{applicant.assessment_reset_count}"
    ).encode("ascii")
    digest = hmac.new(secret.encode("utf-8"), preimage, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _state(applicant: Applicant, now: datetime) -> AssessmentState:
    if applicant.assessment_taken_at is not None:
        return "completed"
    if applicant.assessment_deadline is not None and applicant.assessment_deadline < now:
        return "expired"
    if applicant.assessment_started_at is not None:
        return "in_progress"
    return "not_started"


def register(
    db: Session,
    *,
    tenant_id: UUID,
    external_ref: str,
    email: str,
    first_name: str,
    last_name: str,
    assessment_bank_id: UUID | None,
    return_url: str,
    now: datetime | None = None,
) -> RegistrationResult:
    """Create once by external_ref; every identical retry returns the same URL."""
    now = now or datetime.now(UTC)
    bank = _resolve_bank(db, tenant_id=tenant_id, requested=assessment_bank_id)
    safe_return_url = validate_return_url(return_url)
    canonical_email = normalize_email(email)

    # Intake is admissions' decision, including the external-ref identity rule.
    # This module owns what happens to the *assessment* once an applicant
    # exists, and nothing about who an applicant is.
    try:
        applicant, created = admissions.submit_external_application(
            db,
            tenant_id=tenant_id,
            external_ref=external_ref,
            email=canonical_email,
            first_name=first_name,
            last_name=last_name,
            source=SOURCE,
        )
    except ConflictError as exc:
        raise RegistrationError("external_ref_conflict", str(exc), status_code=409) from exc
    if applicant.assessment_bank_id is not None and applicant.assessment_bank_id != bank.id:
        raise RegistrationError(
            "external_ref_conflict",
            "external_ref is already registered with a different assessment bank",
            status_code=409,
        )
    if applicant.assessment_return_url is not None and applicant.assessment_return_url != safe_return_url:
        raise RegistrationError(
            "external_ref_conflict",
            "external_ref is already registered with a different return URL",
            status_code=409,
        )
    if applicant.assessment_started_at is not None and applicant.source != SOURCE:
        raise RegistrationError(
            "external_ref_conflict",
            "An existing assessment cannot be adopted after it has started",
            status_code=409,
        )

    applicant.source = SOURCE
    applicant.assessment_bank_id = bank.id
    applicant.assessment_return_url = safe_return_url
    if created or applicant.assessment_started_at is None:
        applicant.first_name = first_name.strip()
        applicant.last_name = last_name.strip()
    if applicant.assessment_deadline is None:
        applicant.assessment_deadline = now + timedelta(days=DEFAULT_DEADLINE_DAYS)

    state = _state(applicant, now)
    if state == "expired":
        raise RegistrationError(
            "assessment_link_expired",
            "The assessment link has expired",
            status_code=409,
        )

    raw = _integration_token(applicant)
    applicant.assessment_token_hash = hash_token(raw)
    db.flush()
    return RegistrationResult(
        assessment_url=(f"{_public_base_url()}/apply/assessment?token={quote(raw, safe='')}"),
        expires_at=applicant.assessment_deadline,
        state=state,
    )
