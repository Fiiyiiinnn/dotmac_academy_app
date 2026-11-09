"""Strict DB-over-env self-profile policy, distinct from mail delivery policy."""

# Ported at new-composition genesis (accounts-core slice).

from dataclasses import dataclass
from typing import cast

from sqlalchemy.orm import Session

from app.admissions_settings import validate_profile_policy_value
from app.services import settings_store


@dataclass(frozen=True, slots=True)
class ProfilePolicy:
    avatar_max_bytes: int
    email_results_default: bool
    email_digest_default: bool

    @property
    def avatar_limit_label(self) -> str:
        # Presentation only; the business bound is configured in bytes.
        whole_mb, remainder = divmod(self.avatar_max_bytes, 1024 * 1024)
        if remainder:
            return f"{self.avatar_max_bytes} bytes"
        return f"{whole_mb} MB"


def profile_policy(db: Session) -> ProfilePolicy:
    settings = settings_store.effective(db, require_store=True)
    return ProfilePolicy(
        avatar_max_bytes=cast(
            int, validate_profile_policy_value("account_avatar_max_bytes", settings.account_avatar_max_bytes)
        ),
        email_results_default=cast(
            bool, validate_profile_policy_value("account_email_results_default", settings.account_email_results_default)
        ),
        email_digest_default=cast(
            bool, validate_profile_policy_value("account_email_digest_default", settings.account_email_digest_default)
        ),
    )
