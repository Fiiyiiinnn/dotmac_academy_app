"""Academy-wide entrance-assessment defaults, keyed by tenant.

These two values used to be columns on `Tenant`. They are a product concern —
which question bank a new applicant sits, and for how long — and `Tenant` is a
platform model the kernel owns (`dotmac_kernel.models.Tenant`). Carrying them
there is what made this repo's tenancy model un-swappable: adopting the kernel's
`Tenant` would have dropped two columns that three call sites read.

So they live here instead, in a table this repo owns, keyed by `tenant_id`. The
rule generalises — a product concern that needs per-tenant configuration gets a
product-owned table, never an extra column on a platform table.

Not `tenant_id` on a shared "config" table either. A generic key/value bag is a
magnet: the next product concern lands in it, and two years later nothing owns
any row. One table per concern, owned by the service that resolves it —
`app.services.entrance_exam` is that service, and it is the only writer.

RLS-protected like every other tenant-keyed table here. `tenants` is exempt
because the resolver reads it before any context exists; this table has no such
reader — every caller runs inside a request, or under the BYPASSRLS admin role
the CLI setter uses.
"""

# Ported at new-composition genesis (a0002 lineage); table shapes unchanged.

from __future__ import annotations

from uuid import UUID

from dotmac_kernel.models import Tenant
from sqlalchemy import ForeignKey, Integer
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, TimestampMixin


class TenantEntranceDefaults(Base, TimestampMixin):
    """The academy-wide tier of the entrance-exam precedence chain.

    The chain is: per-applicant snapshot, then cohort override, then this. A row
    here is optional — its absence means the academy has set no default, which
    is a valid state and not an error (see `resolve_bank_id`).
    """

    __tablename__ = "tenant_entrance_defaults"

    # The tenant IS the key: one row per academy, or none.
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(Tenant.__table__.c.id, ondelete="CASCADE"),
        primary_key=True,
    )
    # No FK to question_banks on purpose: banks are tenant-scoped under RLS and
    # this table is read before a scope exists, so the constraint could not be
    # verified from here. `resolve_bank_id` validates on use.
    default_bank_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    default_time_limit_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
