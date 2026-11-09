"""Compose one immutable Learning new-admission state; no authority or facts."""

# Ported at new-composition genesis (learning-facing wave).

from dotmac_learning import AdmissionEnrollmentInitialState
from sqlalchemy.orm import Session

from app.admissions_settings import ADMISSION_ENROLLMENT_POLICY_KEYS, validate_admission_enrollment_policy_value
from app.services.settings_store import effective


def admission_enrollment_policy(db: Session) -> AdmissionEnrollmentInitialState:
    configured = effective(db, require_store=True)
    values = {}
    for key in ADMISSION_ENROLLMENT_POLICY_KEYS:
        try:
            raw = getattr(configured, key)
        except AttributeError as exc:
            raise ValueError(f"missing effective admission policy: {key}") from exc
        values[key] = validate_admission_enrollment_policy_value(key, raw)
    return AdmissionEnrollmentInitialState(
        role_in_cohort=values["admission_enrollment_initial_role"],
        status=values["admission_enrollment_initial_status"],
    )
