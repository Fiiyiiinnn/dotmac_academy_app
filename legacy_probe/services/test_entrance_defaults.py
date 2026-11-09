"""The academy entrance defaults live in their own table, not on `Tenant`.

They used to be two columns on `Tenant`. That is a product concern on a platform
model, and it is the single reason this repo's tenancy model could not be swapped
for `dotmac_kernel.models.Tenant` — adopting the kernel's would have dropped two
columns that three call sites read.

`test_tenant_carries_no_product_columns` is the one that matters long-term. It
fails the moment someone adds the next product field to `Tenant`, which is how
the old ones got there.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.orm import Session

from app.models.entrance_defaults import TenantEntranceDefaults
from app.models.tenant import Tenant
from app.services import entrance_exam


def test_tenant_carries_no_product_columns() -> None:
    """`Tenant` must stay swappable for the kernel's.

    The kernel's Tenant has id, slug, name, is_active, suspended_at, deleted_at
    and timestamps. Anything beyond that here is a product concern that belongs
    in a table this repo owns.
    """
    kernel_shape = {
        "id",
        "slug",
        "name",
        "is_active",
        "suspended_at",
        "deleted_at",
        "created_at",
        "updated_at",
    }
    ours = {c.name for c in Tenant.__table__.columns}
    assert ours <= kernel_shape, (
        f"Tenant has product columns the kernel's does not: {sorted(ours - kernel_shape)}. "
        "Put per-tenant product config in a product-owned table keyed by tenant_id."
    )


def test_absent_row_means_no_default(admin_session: Session, tenant_a) -> None:
    """No row is a valid state, not an error — the academy simply has no default."""
    assert entrance_exam.academy_default_bank_id(admin_session, tenant_id=tenant_a.id) is None
    assert entrance_exam.academy_default_time_limit(admin_session, tenant_id=tenant_a.id) is None


def test_set_then_read(admin_session: Session, tenant_a) -> None:
    bank_id = uuid.uuid4()
    entrance_exam.set_academy_defaults(
        admin_session, tenant_id=tenant_a.id, bank_id=bank_id, time_limit_minutes=45
    )
    admin_session.flush()

    assert entrance_exam.academy_default_bank_id(admin_session, tenant_id=tenant_a.id) == bank_id
    assert entrance_exam.academy_default_time_limit(admin_session, tenant_id=tenant_a.id) == 45
    admin_session.rollback()


def test_set_is_an_upsert_not_a_duplicate(admin_session: Session, tenant_a) -> None:
    """tenant_id is the primary key; setting twice must update, not collide."""
    first, second = uuid.uuid4(), uuid.uuid4()
    entrance_exam.set_academy_defaults(admin_session, tenant_id=tenant_a.id, bank_id=first)
    entrance_exam.set_academy_defaults(
        admin_session, tenant_id=tenant_a.id, bank_id=second, time_limit_minutes=20
    )
    admin_session.flush()

    rows = admin_session.query(TenantEntranceDefaults).filter_by(tenant_id=tenant_a.id).all()
    assert len(rows) == 1
    assert rows[0].default_bank_id == second
    assert rows[0].default_time_limit_minutes == 20
    admin_session.rollback()


def test_omitting_the_time_limit_clears_it(admin_session: Session, tenant_a) -> None:
    """The setter takes the whole configuration, so an omitted limit means untimed.

    Pinned because the alternative — leaving a stale limit behind — is the kind
    of thing that only shows up as an applicant being cut off mid-sitting.
    """
    entrance_exam.set_academy_defaults(
        admin_session, tenant_id=tenant_a.id, bank_id=uuid.uuid4(), time_limit_minutes=30
    )
    entrance_exam.set_academy_defaults(admin_session, tenant_id=tenant_a.id, bank_id=uuid.uuid4())
    admin_session.flush()

    assert entrance_exam.academy_default_time_limit(admin_session, tenant_id=tenant_a.id) is None
    admin_session.rollback()


def test_defaults_do_not_leak_between_tenants(admin_session: Session, tenant_a, tenant_b) -> None:
    bank_id = uuid.uuid4()
    entrance_exam.set_academy_defaults(admin_session, tenant_id=tenant_a.id, bank_id=bank_id)
    admin_session.flush()

    assert entrance_exam.academy_default_bank_id(admin_session, tenant_id=tenant_b.id) is None
    admin_session.rollback()


@pytest.mark.parametrize("name", ["academy_default_bank_id", "academy_default_time_limit"])
def test_readers_are_on_the_owning_service(name: str) -> None:
    """One owner. If a caller reaches past these, the table has two authorities."""
    assert hasattr(entrance_exam, name)
