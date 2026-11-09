"""Hosted qualification for a0006 ERP slice.

Checkpoint tables exist with forced tenant RLS; inbound HMAC signing
round-trips and refuses tampered bodies.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from app.services.erp_integration_security import IntegrationAuthError, sign_request, verify_request

TABLES = ("academy_erp_completion_checkpoints", "academy_erp_imported_completion_keys")


def test_erp_tables_genesis(genesis_database):
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


def test_hmac_roundtrip_and_tamper_refused(monkeypatch):
    from app.config import settings

    body = b'{"event":"completion"}'
    # Disabled by default: no secret configured -> fail closed.
    with pytest.raises(IntegrationAuthError, match="integration_disabled"):
        verify_request(body=body, timestamp="0", signature="0" * 64)
    monkeypatch.setattr(settings, "erp_inbound_hmac_secret", "unit-secret")
    ts = str(int(datetime.now(UTC).timestamp()))
    signature = sign_request(secret="unit-secret", timestamp=ts, body=body)
    verify_request(body=body, timestamp=ts, signature=signature)
    with pytest.raises(IntegrationAuthError):
        verify_request(body=b'{"event":"forged"}', timestamp=ts, signature=signature)
