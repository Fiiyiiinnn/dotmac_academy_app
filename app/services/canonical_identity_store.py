"""Academy profile storage over public canonical identity primitives.

Facts are observations, never authorization or lock proof. Calling product
owners retain Session/root, policy, common Party locks and transaction decisions.
The canonical owner performs the first two flushes; Academy owns the third.
"""

# Ported at new-composition genesis (accounts-core slice).

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from dotmac_kernel.identity_storage import (
    GrantFact as GrantFact,
)
from dotmac_kernel.identity_storage import (
    NewPersonValues as _CanonicalNewPersonValues,
)

# Explicit compatibility re-exports preserve existing helper callers.
from dotmac_kernel.identity_storage import (
    PartyFact as PartyFact,
)
from dotmac_kernel.identity_storage import (
    PersonFact as PersonFact,
)
from dotmac_kernel.identity_storage import (
    RoleFact as RoleFact,
)
from dotmac_kernel.identity_storage import (
    create_person as _create_person_rows,
)
from dotmac_kernel.identity_storage import (
    credential_member_ids as credential_member_ids,
)
from dotmac_kernel.identity_storage import (
    delete_person as delete_person,
)
from dotmac_kernel.identity_storage import (
    ensure_grant as ensure_grant,
)
from dotmac_kernel.identity_storage import (
    find_party_exact_email as find_party_exact_email,
)
from dotmac_kernel.identity_storage import (
    find_party_normalized_email as find_party_normalized_email,
)
from dotmac_kernel.identity_storage import (
    lock_grants as lock_grants,
)
from dotmac_kernel.identity_storage import (
    lock_parties as lock_parties,
)
from dotmac_kernel.identity_storage import (
    lock_person as lock_person,
)
from dotmac_kernel.identity_storage import (
    lock_roles as lock_roles,
)
from dotmac_kernel.identity_storage import (
    provision_missing_roles as provision_missing_roles,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.identity_extensions import AcademyAccountProfile


@dataclass(frozen=True, slots=True)
class ProfileFact:
    party_id: UUID
    tenant_id: UUID
    status: str
    avatar_path: str | None
    prefs: object  # Detached deep copy; not a deeply immutable JSON capability.


@dataclass(frozen=True, slots=True)
class NewPersonValues:
    """Caller-decided values, with no normalization or business defaults."""

    id: UUID
    tenant_id: UUID
    email: str | None
    display_name: str
    first_name: str
    last_name: str
    is_active: bool
    custom_fields: dict[str, object]
    profile_status: str
    profile_avatar_path: str | None
    profile_prefs: object
    created_at: datetime
    updated_at: datetime


def lock_profile(db: Session, *, tenant_id: UUID, party_id: UUID) -> ProfileFact | None:
    row = db.scalars(
        select(AcademyAccountProfile)
        .where(AcademyAccountProfile.tenant_id == tenant_id, AcademyAccountProfile.party_id == party_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    return (
        None
        if row is None
        else ProfileFact(row.party_id, row.tenant_id, row.status, row.avatar_path, deepcopy(row.prefs))
    )


def create_person(db: Session, *, values: NewPersonValues) -> PartyFact:
    """Three source flushes; caller owns authorization, collision handling and rollback."""
    party = _create_person_rows(
        db,
        values=_CanonicalNewPersonValues(
            id=values.id,
            tenant_id=values.tenant_id,
            email=values.email,
            display_name=values.display_name,
            first_name=values.first_name,
            last_name=values.last_name,
            is_active=values.is_active,
            custom_fields=values.custom_fields,
            created_at=values.created_at,
            updated_at=values.updated_at,
        ),
    )
    db.add(
        AcademyAccountProfile(
            tenant_id=values.tenant_id,
            party_id=values.id,
            status=values.profile_status,
            avatar_path=values.profile_avatar_path,
            prefs=deepcopy(values.profile_prefs),
            created_at=values.created_at,
            updated_at=values.updated_at,
        )
    )
    db.flush()
    return PartyFact(party.id, party.tenant_id, party.party_type, party.email, party.display_name, party.is_active)
