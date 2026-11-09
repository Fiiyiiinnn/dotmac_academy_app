"""Hosted qualification for the a0003 identity lineage.

Genesis creates the account-profile / credential-policy / notification
tables with forced tenant RLS; reflected columns match the ported ORM
models exactly (DDL was rendered from them and frozen).
"""

from __future__ import annotations

from sqlalchemy import inspect, text

from app.models.identity_extensions import IdentityExtensionBase
from app.models.notification import Notification

TABLES = ("academy_account_profiles", "academy_credential_policies", "notifications")


def test_identity_tables_rls_and_model_alignment(genesis_database):
    admin, _superuser, _name = genesis_database
    with admin.connect() as conn:
        rows = {
            r[0]: (r[1], r[2])
            for r in conn.execute(
                text("SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = ANY(:t)"),
                {"t": list(TABLES)},
            )
        }
        assert set(rows) == set(TABLES)
        assert all(v == (True, True) for v in rows.values())
        # Head tracking lives in the newest lineage test.
    insp = inspect(admin)
    for name in TABLES:
        reflected = {c["name"] for c in insp.get_columns(name)}
        if name == "notifications":
            model_cols = {c.name for c in Notification.__table__.columns}
        else:
            model_cols = {c.name for c in IdentityExtensionBase.metadata.tables[f"public.{name}"].columns}
        assert reflected == model_cols, name
