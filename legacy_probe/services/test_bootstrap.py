"""Tenant bootstrap creates the three standard roles and grants the admin role to the owner.

Role admission guard: the web facets admit student/instructor/admin only, so a bootstrap
that drifts from exactly these three roles, or grants the owner anything but admin, must fail here.
The owner-credential shape (Party/PartyPerson/profile/credential extension) is asserted by
tests/services/test_bootstrap_identity.py, which owns the offline runtime fixture used below.
"""

from __future__ import annotations

from uuid import uuid4

from dotmac_kernel.models import Party, PartyRoleGrant, Role
from sqlalchemy import select

from app.services.bootstrap import bootstrap_tenant
from legacy_probe.services.test_bootstrap_identity import offline_runtime  # noqa: F401  (fixture)


def test_bootstrap_creates_the_three_standard_roles_and_one_admin_grant(offline_runtime):  # noqa: F811
    # The dedicated database (see offline_runtime) is never cleaned: nothing is
    # deleted, audit immutability holds, and the shared database never sees it.
    slug = "bootstrap-roles-" + uuid4().hex
    with offline_runtime.platform_session() as db:
        result = bootstrap_tenant(
            db, slug=slug, name="Roles Tenant", admin_email=slug + "@example.test",
            admin_password="hosted-bootstrap-secret-923!",
        )
        tid, pid = result.id, result.owner_party_id
    with offline_runtime.platform_session() as db:
        offline_runtime.set_tenant(db, tid, transaction_local=True)
        roles = db.scalars(select(Role).where(Role.tenant_id == tid)).all()
        assert {r.slug for r in roles} == {"student", "instructor", "admin"}
        assert len(roles) == 3, "no duplicate role rows"
        grants = db.scalars(select(PartyRoleGrant).where(PartyRoleGrant.tenant_id == tid)).all()
        assert len(grants) == 1, "the owner receives exactly one grant"
        assert grants[0].party_id == pid
        assert db.get(Role, grants[0].role_id).slug == "admin"
        assert db.get(Party, pid).tenant_id == tid
