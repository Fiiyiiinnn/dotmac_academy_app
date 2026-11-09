"""Bootstrap identity grants for the offline platform pair (a0009).

Tenant bootstrap runs through the `platform_api` session and its
product catalogue admission (`bootstrap_identity._catalog`) requires
SELECT+INSERT on the identity extension tables; a0003 granted them
only to `app_user`, so bootstrap refused every fresh genesis. Mirror
the `app_user` CRUD grant for `platform_api` (issue #2 finding).
"""

from __future__ import annotations

from alembic import op

revision = "a0009_bootstrap_identity_grants"
down_revision = "a0008_worker_acl_alignment"
branch_labels = None
depends_on = None

_TABLES = ("academy_account_profiles", "academy_credential_policies")


def upgrade() -> None:
    for table in _TABLES:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON public.{table} TO platform_api")


def downgrade() -> None:
    for table in _TABLES:
        op.execute(f"REVOKE SELECT, INSERT, UPDATE, DELETE ON public.{table} FROM platform_api")
