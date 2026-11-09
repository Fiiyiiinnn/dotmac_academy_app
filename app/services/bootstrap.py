"""Academy CLI compatibility facade over the sole Kernel bootstrap owner."""

# Ported at new-composition genesis (learning-facing wave).

from dataclasses import dataclass
from datetime import datetime
from typing import cast
from uuid import UUID

from dotmac_kernel.identity_provisioning import BootstrapTenantV1
from dotmac_kernel.identity_provisioning import bootstrap_tenant as _bootstrap_tenant
from sqlalchemy.orm import Session

from app.services.identity import normalize_email


@dataclass(frozen=True, slots=True)
class AcademyBootstrapTenantResult:
    id: UUID
    slug: str
    owner_party_id: UUID


def bootstrap_tenant(db: Session, *, slug: str, name: str, admin_email: str, admin_password: str):
    result = _bootstrap_tenant(
        db,
        BootstrapTenantV1(slug=slug, name=name, owner_email=normalize_email(admin_email)),
        password=admin_password,
    )
    return AcademyBootstrapTenantResult(result.tenant_id, result.slug, result.owner_party_id)


def ensure_roles(db: Session, tenant_id: UUID):
    """Compatibility storage helper; authorization remains with its existing caller.

    Product-configured web role names retain the historical additive behavior;
    existing roles are returned unchanged, including roles outside that catalog.
    """
    from uuid import uuid4

    from dotmac_kernel.identity_storage import RoleFact, provision_missing_roles
    from dotmac_kernel.models import Role
    from sqlalchemy import func, select

    from app.services.settings_store import effective

    configured = effective(db, require_store=True).web_account_creation_role_provisioning
    existing = tuple(
        db.scalars(
            select(Role)
            .where(Role.tenant_id == tenant_id)
            .order_by(Role.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    )
    by_slug = {row.slug: row for row in existing}
    # Legacy TimestampMixin uses server now(): PostgreSQL transaction time.
    now = cast(datetime, db.scalar(select(func.transaction_timestamp())))
    missing = {slug: uuid4() for slug in configured if slug not in by_slug}
    expected_ids = {row.id for row in existing} | set(missing.values())
    provision_missing_roles(
        db,
        tenant_id=tenant_id,
        catalog=tuple(RoleFact(row.id, row.tenant_id, row.slug, row.name) for row in existing),
        configured_names=configured,
        missing_role_ids=missing,
        created_at=now,
        updated_at=now,
    )
    rows = tuple(
        db.scalars(
            select(Role)
            .where(Role.tenant_id == tenant_id, Role.id.in_(expected_ids))
            .order_by(Role.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    )
    if {row.id for row in rows} != expected_ids:
        raise ValueError("configured role storage outcome differs")
    return {row.slug: row for row in rows}
