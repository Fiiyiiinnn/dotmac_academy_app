"""Shared role lookup.

`role_slugs` returns the set of role slugs a person holds within a tenant. It is
the single source of truth for role membership used by the web auth gates, the
account routes, and the nav context processor.
"""

# Ported at new-composition genesis (identity/facilitator slice).

from __future__ import annotations

from uuid import UUID

from dotmac_kernel.models import PartyRoleGrant, Role
from sqlalchemy import select
from sqlalchemy.orm import Session


def role_slugs(db: Session, tenant_id: UUID, person_id: UUID) -> set[str]:
    """Return the set of role slugs held by the person within the tenant."""
    rows = db.scalars(
        select(Role.slug)
        .join(
            PartyRoleGrant,
            (PartyRoleGrant.role_id == Role.id) & (PartyRoleGrant.tenant_id == Role.tenant_id),
        )
        .where(Role.tenant_id == tenant_id)
        .where(PartyRoleGrant.tenant_id == tenant_id)
        .where(PartyRoleGrant.party_id == person_id)
    ).all()
    return set(rows)
