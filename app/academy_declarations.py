"""Shared route-free Academy declaration for web and migration composition."""

# Ported at new-composition genesis (composition wave).

from dotmac_kernel.features import FeatureManifest
from dotmac_kernel.permissions import PermissionSpec

from app.api_identity_declarations import API_IDENTITY_AUDIT_ACTIONS
from app.settings_specs import FLOAT_VALUE_TYPE

FACILITATOR_AUDIT_ACTIONS = (
    "facilitator_nomination.create",
    "facilitator_nomination.activate",
    "facilitator_nomination.revoke",
)

# Written by the Academy credential adapters (app/services/credential_lifecycle.py,
# app/services/password_change.py). Kernel a107 reserves only identity.history_imported
# and credential.password.assigned, so the product declares these four.
CREDENTIAL_LIFECYCLE_AUDIT_ACTIONS = (
    "credential.sessions.revoked",
    "credential.reset.completed",
    "credential.reset_tokens.invalidated",
    "credential.password.changed",
)

ACADEMY_FEATURE_DECLARATION = FeatureManifest(
    name="academy",
    audit_actions=(
        *API_IDENTITY_AUDIT_ACTIONS,
        *FACILITATOR_AUDIT_ACTIONS,
        *CREDENTIAL_LIFECYCLE_AUDIT_ACTIONS,
        "lab_result.record",
    ),
    permissions=(
        PermissionSpec(
            code="academy.web.access",
            description="Enter the Academy web portal.",
            default_roles=("student", "instructor", "admin"),
        ),
    ),
    # ADR-0003 settings adoption: the academy domain already backstops
    # app.settings_store rows; declaring it lets the kernel resolver and
    # shadow sweep address them. The float value type is Academy-owned
    # (the kernel ships none) and declared in app.settings_specs.
    setting_domains=("academy",),
    setting_value_types=(FLOAT_VALUE_TYPE,),
)
