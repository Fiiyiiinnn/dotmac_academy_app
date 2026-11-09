"""Immutable Settings-backed onboarding decisions; no authority."""

# Ported at new-composition genesis (admissions services slice).

from dataclasses import dataclass
from types import MappingProxyType
from typing import cast

from app.admissions_settings import validate_onboarding_policy_value, validate_web_account_creation_role_provisioning
from app.services.settings_store import effective


@dataclass(frozen=True, slots=True)
class OnboardingPolicy:
    global_role: str
    task_catalog: tuple[tuple[str, str], ...]
    role_provisioning: object


def onboarding_policy(db):
    configured = effective(db, require_store=True)
    return OnboardingPolicy(
        cast(
            str,
            validate_onboarding_policy_value(
                "onboarding_enrollment_global_role", configured.onboarding_enrollment_global_role
            ),
        ),
        cast(
            tuple[tuple[str, str], ...],
            validate_onboarding_policy_value("onboarding_task_catalog", configured.onboarding_task_catalog),
        ),
        MappingProxyType(
            validate_web_account_creation_role_provisioning(configured.web_account_creation_role_provisioning)
        ),
    )
