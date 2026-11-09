"""Composable Academy account-reset policy, supplied explicitly to the owner."""

# Ported at new-composition genesis (learning-facing wave).

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from dotmac_kernel.credential_lifecycle import CredentialPolicyViolation, CredentialSubjectRef
from sqlalchemy.orm import Session

from app.admissions_settings import validate_authentication_policy_value
from app.services.settings_store import effective


@dataclass(frozen=True, slots=True)
class AcademyPasswordPolicy:
    """Reset-only rule implementing the early and per-member policy ports.

    This policy owns no default: configuration chooses the positive minimum.
    No password material is retained or interpolated into an error.
    """

    minimum_length: int

    def __post_init__(self) -> None:
        if type(self.minimum_length) is not int or self.minimum_length < 1:
            raise ValueError("minimum_length must be a positive integer")

    def validate_account(self, secret: str) -> None:
        """Refuse before token proof or account mutation."""
        if not isinstance(secret, str) or len(secret) < self.minimum_length:
            raise CredentialPolicyViolation("Password does not meet the configured reset policy")

    def validate(self, subject: CredentialSubjectRef, secret: str) -> None:
        """Apply the same configured rule to each actual credential member."""
        if not isinstance(subject, CredentialSubjectRef):
            raise CredentialPolicyViolation("Credential subject is required for reset policy")
        self.validate_account(secret)


def password_reset_policy(db: Session) -> AcademyPasswordPolicy:
    """Read actual DB-over-env settings, refusing unreadable/malformed state."""
    configured = effective(db, require_store=True)
    return AcademyPasswordPolicy(configured.password_reset_min_length)


@dataclass(frozen=True, slots=True)
class PasswordResetTokenPolicy:
    """Required configured lifetime; zero/fractional native rounding is retained."""

    minutes: float

    def __post_init__(self) -> None:
        validated = validate_authentication_policy_value("password_reset_token_minutes", self.minutes)
        object.__setattr__(self, "minutes", float(validated))

    def deadline(self, now: datetime) -> datetime:
        """Check calendar representability against the actual post-lock time."""
        try:
            return now + timedelta(minutes=self.minutes)
        except OverflowError:
            raise ValueError("password reset token deadline is not representable") from None


def password_reset_token_policy(db: Session) -> PasswordResetTokenPolicy:
    configured = effective(db, require_store=True)
    return PasswordResetTokenPolicy(configured.password_reset_token_minutes)
