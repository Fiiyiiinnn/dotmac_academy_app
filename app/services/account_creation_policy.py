"""Configured web account role admission and chosen-password policy.

This policy supplies rules, not authenticated authority or role-grant evidence.
"""

# Ported at new-composition genesis (accounts-core slice).

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from dotmac_kernel.credential_lifecycle import CredentialPolicyViolation, CredentialSubjectRef
from sqlalchemy.orm import Session

from app.admissions_settings import (
    validate_web_account_creation_role_admission,
    validate_web_account_creation_role_provisioning,
)
from app.services.settings_store import effective


def _actor_roles(actor_role_slugs: Collection[str]) -> frozenset[str]:
    """Require role collections; never interpret one string as a collection."""
    if isinstance(actor_role_slugs, (str, bytes)) or not isinstance(actor_role_slugs, Collection):
        raise ValueError("actor role slugs must be a collection of nonempty role names")
    if any(type(role) is not str or not role or role != role.strip() for role in actor_role_slugs):
        raise ValueError("actor role slugs must be a collection of nonempty role names")
    return frozenset(actor_role_slugs)


@dataclass(frozen=True, slots=True)
class AcademyWebAccountCreationPolicy:
    """Immutable configured rules; all three inputs are required with no defaults.

    Subject/role existence, current authenticated actor identity, locked grants,
    transaction authority and audit attribution belong to the installed adapter.
    """

    role_admission: Mapping[str, tuple[str, ...]]
    minimum_length: int
    role_provisioning: Mapping[str, str]

    def __post_init__(self) -> None:
        if not isinstance(self.role_admission, Mapping):
            raise ValueError(
                "web_account_creation_role_admission must map role names to nonempty unique actor-role lists"
            )
        configured = validate_web_account_creation_role_admission(dict(self.role_admission))
        if type(self.minimum_length) is not int or self.minimum_length < 0:
            raise ValueError("web_account_creation_password_min_length must be a nonnegative integer")
        minimum = self.minimum_length
        if not isinstance(self.role_provisioning, Mapping):
            raise ValueError("web_account_creation_role_provisioning must map role names to nonempty display names")
        provisioning = validate_web_account_creation_role_provisioning(dict(self.role_provisioning))
        object.__setattr__(self, "role_provisioning", MappingProxyType(provisioning))
        object.__setattr__(self, "role_admission", MappingProxyType(configured))
        object.__setattr__(self, "minimum_length", minimum)

    def can_create_any(self, actor_role_slugs: Collection[str]) -> bool:
        """Admission predicate only; the caller must verify the actor's real grants."""
        actors = _actor_roles(actor_role_slugs)
        return any(actors.intersection(allowed) for allowed in self.role_admission.values())

    def can_assign_role(self, target_role: str, actor_role_slugs: Collection[str]) -> bool:
        """Unknown targets return false; target validation/order is the caller's."""
        actors = _actor_roles(actor_role_slugs)
        allowed = self.role_admission.get(target_role)
        return allowed is not None and bool(actors.intersection(allowed))

    def validate_account(self, secret: str) -> None:
        """Optional early check using the same configured password rule."""
        if not isinstance(secret, str) or len(secret) < self.minimum_length:
            raise CredentialPolicyViolation("Password does not meet the configured web account creation policy")

    def validate(self, subject: CredentialSubjectRef, secret: str) -> None:
        """Implement the Kernel PasswordPolicyPort for an actual subject."""
        if not isinstance(subject, CredentialSubjectRef):
            raise CredentialPolicyViolation("Credential subject is required for web account creation policy")
        self.validate_account(secret)


def web_account_creation_policy(db: Session) -> AcademyWebAccountCreationPolicy:
    """Read actual DB-over-env rules, refusing missing/unreadable/malformed state."""
    configured = effective(db, require_store=True)
    return AcademyWebAccountCreationPolicy(
        configured.web_account_creation_role_admission,
        configured.web_account_creation_password_min_length,
        configured.web_account_creation_role_provisioning,
    )
