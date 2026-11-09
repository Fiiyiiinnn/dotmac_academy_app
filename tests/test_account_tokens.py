"""Hosted qualification for a0004 + accounts-core policies.

`account_tokens` exists with forced tenant RLS; authentication and
initial-identity policies compose from legacy defaults.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.authentication_policy import authentication_policy
from app.services.initial_identity_policy import initial_identity_policy


def test_account_tokens_genesis_and_policies(genesis_database):
    admin, _superuser, _name = genesis_database
    with admin.connect() as conn:
        row = conn.execute(
            text("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = 'account_tokens'")
        ).first()
        assert row == (True, True)
        # Head tracking lives in the newest lineage test.
    with Session(admin) as db:
        auth = authentication_policy(db)
        assert auth.maximum_failures == 5
        assert auth.lockout_minutes == 15.0
        initial = initial_identity_policy(db)
        assert initial.is_active is True
        assert initial.profile_status == "active"
