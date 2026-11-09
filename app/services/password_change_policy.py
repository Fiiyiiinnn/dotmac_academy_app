"""Resolved password-change purpose configuration; source only, not activation.

The future Kernel authenticated-account operation owns confirmation, typed
refusal and credential effects. This wrapper contains configuration only: no
password material, verifier, authorization, callbacks or business defaults.
"""

# Ported at new-composition genesis (learning-facing wave 2).

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.admissions_settings import validate_password_change_policy_value
from app.services import settings_store


@dataclass(frozen=True, slots=True)
class PasswordChangePolicy:
    min_length: int

    def __post_init__(self) -> None:
        # Explicit construction is held to the same strict representation as
        # Settings/store; never silently coerce a caller's policy object.
        validated = validate_password_change_policy_value("password_change_min_length", self.min_length)
        if type(self.min_length) is not int or validated != self.min_length:
            raise ValueError("min_length must be a nonnegative integer")


def password_change_policy(db: Session) -> PasswordChangePolicy:
    configured = settings_store.effective(db, require_store=True)
    return PasswordChangePolicy(
        min_length=validate_password_change_policy_value(
            "password_change_min_length", configured.password_change_min_length
        )
    )
