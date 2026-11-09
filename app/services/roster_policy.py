"""Compose future Learning bulk initial facts; this factory grants no authority.

Source dependency: Learning must export BulkEnrollmentInitialState separately
from AdmissionEnrollmentInitialState and InvitationEnrollmentInitialState.
Call only after genuine human authorization and actual cohort existence checks.
"""

# Ported at new-composition genesis (learning-facing wave).

from dotmac_learning import BulkEnrollmentInitialState
from sqlalchemy.orm import Session

from app.admissions_settings import BULK_ENROLLMENT_POLICY_KEYS, validate_enrollment_initial_state_policy_value
from app.services.settings_store import effective


def bulk_enrollment_policy(db: Session) -> BulkEnrollmentInitialState:
    """Pin both required new-row facts from one strict DB-over-env snapshot."""
    configured = effective(db, require_store=True)
    values = {}
    for key in BULK_ENROLLMENT_POLICY_KEYS:
        try:
            raw = getattr(configured, key)
        except AttributeError as exc:
            raise ValueError(f"missing effective bulk enrollment policy: {key}") from exc
        values[key] = validate_enrollment_initial_state_policy_value(key, raw)
    return BulkEnrollmentInitialState(
        role_in_cohort=values["bulk_enrollment_initial_role"],
        status=values["bulk_enrollment_initial_status"],
    )
