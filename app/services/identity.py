"""Canonical account identity helpers.

Kernel Party and PartyPerson supply canonical identity. Academy-owned credential
policy retains compatible aliases; all account credentials still participate in
password and lockout transitions. Legacy Person lookup and credential-row
synchronization helpers have no callers in this prepared assembly and are retired.
"""

# Ported at new-composition genesis (admissions services slice).

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from dotmac_kernel.identity import normalize_email as normalize_identity_email
from sqlalchemy import func, select
from sqlalchemy.orm import Session


def normalize_email(email: str | None) -> str:
    """Retain Academy trimming and delegate identity casing to its Kernel owner."""
    return normalize_identity_email((email or "").strip())


def canonical_party_for_email(db: Session, *, tenant_id: UUID, email: str, lock: bool = False):
    """Canonical lookup for sign-in only; legacy mutable Person callers are gated."""
    from dotmac_kernel.models import Party

    canonical = normalize_email(email)
    if not canonical:
        return None
    statement = select(Party).where(Party.tenant_id == tenant_id, func.lower(func.btrim(Party.email)) == canonical)
    if lock:
        statement = statement.with_for_update(of=Party)
    return db.scalars(statement.execution_options(populate_existing=True)).first()


def sync_canonical_credential_emails(db: Session, *, party, now: datetime) -> None:
    """Preserve unordered compatible-alias selection in the product policy table."""
    from dotmac_kernel.models import UserCredential as CanonicalCredential

    from app.models.identity_extensions import AcademyCredentialPolicy

    credentials = list(
        db.scalars(
            select(CanonicalCredential)
            .where(CanonicalCredential.tenant_id == party.tenant_id, CanonicalCredential.party_id == party.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    )
    policies = list(
        db.scalars(
            select(AcademyCredentialPolicy)
            .where(
                AcademyCredentialPolicy.tenant_id == party.tenant_id,
                AcademyCredentialPolicy.credential_id.in_([row.id for row in credentials]),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    )
    by_id = {row.credential_id: row for row in policies}
    if not credentials or len(by_id) != len(credentials):
        from dotmac_kernel.credential_lifecycle import CredentialCohortDrift

        raise CredentialCohortDrift("complete persisted alias policy is required")
    canonical = normalize_email(party.email)
    selected = next((row for row in credentials if normalize_email(by_id[row.id].email) == canonical), credentials[0])
    party.email = canonical
    if by_id[selected.id].email != canonical:
        by_id[selected.id].email = canonical
        by_id[selected.id].updated_at = now
    db.flush()


@dataclass(frozen=True, slots=True)
class AcademyCurrentAccount:
    """Live request projection of a genuine Party; never writer authorization.

    Fields are read anew for each guard invocation. This immutable object is
    neither a mutable legacy Person nor a capability for a later action. Writer
    ports must revalidate current session/principal/grants under their locks.
    """

    id: UUID
    tenant_id: UUID
    email: str
    first_name: str
    last_name: str
    status: str
    avatar_path: str | None
    _prefs: object = field(repr=False)

    @property
    def prefs(self) -> object:
        """Return genuine JSON values without exposing snapshot or ORM aliases."""
        return deepcopy(self._prefs)


def current_account(db: Session, *, tenant_id: UUID, party_id: UUID) -> AcademyCurrentAccount | None:
    """Read one fresh tenant-local active person account from genuine records."""
    from dotmac_kernel.models import Party, PartyPerson, PartyType

    from app.models.identity_extensions import AcademyAccountProfile

    row = db.execute(
        select(Party, PartyPerson, AcademyAccountProfile)
        .select_from(Party)
        .join(PartyPerson, PartyPerson.party_id == Party.id)
        .join(
            AcademyAccountProfile,
            (AcademyAccountProfile.party_id == Party.id) & (AcademyAccountProfile.tenant_id == Party.tenant_id),
        )
        .where(
            Party.tenant_id == tenant_id,
            Party.id == party_id,
            Party.party_type == PartyType.person,
            Party.is_active.is_(True),
            AcademyAccountProfile.tenant_id == tenant_id,
            AcademyAccountProfile.status == "active",
        )
        .execution_options(populate_existing=True)
    ).first()
    if row is None:
        return None
    party, person, profile = row
    if any(type(value) is not str for value in (party.email, person.first_name, person.last_name, profile.status)):
        return None
    if not party.email.strip():
        return None
    return AcademyCurrentAccount(
        party.id,
        party.tenant_id,
        party.email,
        person.first_name,
        person.last_name,
        profile.status,
        profile.avatar_path,
        deepcopy(profile.prefs),
    )
