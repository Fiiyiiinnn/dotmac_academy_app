"""Settings-backed product policy for Learning invitation enrollment roles."""

# Ported at new-composition genesis (admissions services slice).

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import cast

from dotmac_learning import InvitationEnrollmentInitialState
from sqlalchemy.orm import Session

from app.admissions_settings import (
    InvitationMemberRole,
    validate_enrollment_initial_state_policy_value,
    validate_invitation_default_member_role,
    validate_invitation_member_role_map,
)
from app.services.settings_store import effective


@dataclass(frozen=True, slots=True)
class AcademyInvitationEnrollmentPolicy:
    """Immutable configured policy; all inputs are required, with no defaults.

    This maps opaque global role names. It neither grants global roles nor
    establishes roster authority or account eligibility.
    """

    role_map: Mapping[str, InvitationMemberRole]
    default_member_role: InvitationMemberRole
    initial_state: InvitationEnrollmentInitialState

    def __post_init__(self) -> None:
        if type(self.initial_state) is not InvitationEnrollmentInitialState:
            raise ValueError("invitation initial state is required")
        # Fresh reconstruction refuses tampered frozen instances before use.
        initial_state = InvitationEnrollmentInitialState(
            status=validate_enrollment_initial_state_policy_value(
                "invitation_enrollment_initial_status", self.initial_state.status
            )
        )
        object.__setattr__(self, "initial_state", initial_state)
        if not isinstance(self.role_map, Mapping):
            raise ValueError("invitation_member_role_map must map nonempty role names to student or instructor")
        configured = validate_invitation_member_role_map(dict(self.role_map))
        fallback = validate_invitation_default_member_role(self.default_member_role)
        object.__setattr__(self, "role_map", MappingProxyType(configured))
        object.__setattr__(self, "default_member_role", fallback)

    def member_role(self, global_role: str) -> InvitationMemberRole:
        return self.role_map.get(global_role, self.default_member_role)


def invitation_enrollment_policy(db: Session) -> AcademyInvitationEnrollmentPolicy:
    """Compose the actual DB-over-env policy; unreadable/malformed state refuses."""
    configured = effective(db, require_store=True)
    try:
        mapping = configured.invitation_member_role_map
        fallback = configured.invitation_default_member_role
        status = configured.invitation_enrollment_initial_status
    except AttributeError as exc:
        raise ValueError("missing effective invitation enrollment policy") from exc
    return AcademyInvitationEnrollmentPolicy(
        mapping,
        fallback,
        InvitationEnrollmentInitialState(
            status=validate_enrollment_initial_state_policy_value("invitation_enrollment_initial_status", status)
        ),
    )


@dataclass(frozen=True, slots=True)
class InvitationIssuancePolicy:
    token_hours: float
    replace_outstanding: bool
    role_provisioning: Mapping[str, str]
    role_admission: Mapping[str, tuple[str, ...]]

    def __post_init__(self):
        from app.admissions_settings import (
            validate_invitation_purpose_policy_value,
            validate_web_account_creation_role_admission,
            validate_web_account_creation_role_provisioning,
        )

        object.__setattr__(
            self, "token_hours", validate_invitation_purpose_policy_value("invitation_token_hours", self.token_hours)
        )
        object.__setattr__(
            self,
            "replace_outstanding",
            validate_invitation_purpose_policy_value("invitation_human_replace_outstanding", self.replace_outstanding),
        )
        object.__setattr__(
            self,
            "role_provisioning",
            MappingProxyType(validate_web_account_creation_role_provisioning(dict(self.role_provisioning))),
        )
        object.__setattr__(
            self,
            "role_admission",
            MappingProxyType(validate_web_account_creation_role_admission(dict(self.role_admission))),
        )

    def deadline(self, now, *, ttl_hours=None):
        from datetime import timedelta

        from app.admissions_settings import validate_invitation_purpose_policy_value

        # Explicit zero, negative and fractional values retain native behavior.
        hours = validate_invitation_purpose_policy_value(
            "invitation_token_hours", self.token_hours if ttl_hours is None else ttl_hours
        )
        try:
            return now + timedelta(hours=hours)
        except (OverflowError, ValueError):
            raise ValueError("invitation expiry is not representable") from None


def invitation_issuance_policy(db: Session, *, purpose: str) -> InvitationIssuancePolicy:
    """Trusted purpose selection; no public caller chooses authority or defaults.

    The stranded policy is prepared independently; its old issuer is explicitly
    not adopted by these human wrappers and retains its separate future gate.
    """
    from app.admissions_settings import (
        validate_invitation_purpose_policy_value,
        validate_web_account_creation_role_admission,
        validate_web_account_creation_role_provisioning,
    )

    configured = effective(db, require_store=True)
    if purpose == "human":
        replace = configured.invitation_human_replace_outstanding
        key = "invitation_human_replace_outstanding"
    elif purpose == "onboarding":
        replace = configured.invitation_onboarding_replace_outstanding
        key = "invitation_onboarding_replace_outstanding"
    elif purpose == "stranded":
        replace = configured.invitation_stranded_replace_outstanding
        key = "invitation_stranded_replace_outstanding"
    else:
        raise ValueError("unsupported invitation issuance purpose")
    return InvitationIssuancePolicy(
        token_hours=validate_invitation_purpose_policy_value(
            "invitation_token_hours", configured.invitation_token_hours
        ),
        replace_outstanding=cast(bool, validate_invitation_purpose_policy_value(key, replace)),
        role_provisioning=MappingProxyType(
            validate_web_account_creation_role_provisioning(configured.web_account_creation_role_provisioning)
        ),
        role_admission=MappingProxyType(
            validate_web_account_creation_role_admission(configured.web_account_creation_role_admission)
        ),
    )


@dataclass(frozen=True, slots=True)
class InvitationAcceptancePolicy:
    minimum_length: int

    def __post_init__(self):
        from app.admissions_settings import validate_invitation_purpose_policy_value

        if type(self.minimum_length) is not int:
            raise ValueError("invitation minimum length must be a positive integer")
        validate_invitation_purpose_policy_value("invitation_password_min_length", self.minimum_length)

    def validate_account(self, secret):
        from dotmac_kernel.credential_lifecycle import CredentialPolicyViolation

        if type(secret) is not str or len(secret) < self.minimum_length:
            raise CredentialPolicyViolation("password does not satisfy invitation acceptance policy")

    def validate(self, subject, secret):
        from dotmac_kernel.credential_lifecycle import CredentialPolicyViolation, CredentialSubjectRef

        if not isinstance(subject, CredentialSubjectRef):
            raise CredentialPolicyViolation("invitation credential subject is required")
        self.validate_account(secret)


def invitation_acceptance_policy(db: Session) -> InvitationAcceptancePolicy:
    """Prepared purpose factory; claim-route composition is separately gated."""
    from app.admissions_settings import validate_invitation_purpose_policy_value

    configured = effective(db, require_store=True)
    return InvitationAcceptancePolicy(
        cast(
            int,
            validate_invitation_purpose_policy_value(
                "invitation_password_min_length", configured.invitation_password_min_length
            ),
        )
    )
