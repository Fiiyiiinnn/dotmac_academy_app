"""Strict DB-over-env sign-in policy; no adapter-owned defaults."""

# Ported at new-composition genesis (accounts-core slice).

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy.orm import Session

from app.admissions_settings import validate_authentication_policy_value
from app.services import settings_store


@dataclass(frozen=True, slots=True)
class AuthenticationPolicy:
    maximum_failures: int
    lockout_minutes: float

    def lockout_until(self, now: datetime) -> datetime:
        """Use native rounding and the exact remaining datetime range."""
        minutes = validate_authentication_policy_value("authentication_lockout_minutes", self.lockout_minutes)
        duration = timedelta(minutes=minutes)
        maximum = datetime.max.replace(tzinfo=now.tzinfo)
        if duration > maximum - now:
            raise ValueError("authentication lockout requires a representable deadline")
        return now + duration


def authentication_policy(db: Session) -> AuthenticationPolicy:
    settings = settings_store.effective(db, require_store=True)
    policy = AuthenticationPolicy(
        maximum_failures=cast(
            int,
            validate_authentication_policy_value(
                "authentication_max_failed_logins", settings.authentication_max_failed_logins
            ),
        ),
        lockout_minutes=float(
            validate_authentication_policy_value(
                "authentication_lockout_minutes", settings.authentication_lockout_minutes
            )
        ),
    )
    # Refuse invalid effective policy before verification or owned mutation.
    policy.lockout_until(datetime.now(UTC))
    return policy
