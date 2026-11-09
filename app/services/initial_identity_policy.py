"""Product-owned initial state, composed once per new identity; no authority."""

# Ported at new-composition genesis (accounts-core slice).

from dataclasses import dataclass

from app.admissions_settings import validate_initial_identity_policy_value
from app.services.settings_store import effective


@dataclass(frozen=True, slots=True)
class InitialIdentityPolicy:
    is_active: bool
    profile_status: str

    def __post_init__(self):
        if type(self.is_active) is not bool:
            raise ValueError("account_initial_is_active has an invalid policy value")
        validate_initial_identity_policy_value("account_initial_profile_status", self.profile_status)


def initial_identity_policy(db) -> InitialIdentityPolicy:
    configured = effective(db, require_store=True)
    return InitialIdentityPolicy(configured.account_initial_is_active, configured.account_initial_profile_status)
