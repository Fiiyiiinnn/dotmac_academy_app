"""Byte-exact ERP -> Academy HMAC request contract."""

from __future__ import annotations

import pytest

from app.config import settings
from app.services.erp_integration_security import (
    IntegrationAuthError,
    sign_request,
    signature_preimage,
    verify_request,
)

SECRET = "test-inbound-secret"
TIMESTAMP = "1786089600"
BODY = (
    b'{"external_ref":"APP-42","email":"ada@example.com",'
    b'"first_name":"Ada","last_name":"Lovelace",'
    b'"return_url":"https://erp.example/recruitment/applications/APP-42"}'
)
SIGNATURE = "sha256=6e97bf8756317381db24f1377a872012cfb41f1969d1d1e5dcfe73fcaab5bc3c"


def _configure(monkeypatch):
    monkeypatch.setattr(settings, "erp_inbound_hmac_secret", SECRET)
    monkeypatch.setattr(settings, "erp_inbound_hmac_max_skew_seconds", 300)


def test_deterministic_signature_vector(monkeypatch):
    _configure(monkeypatch)
    assert signature_preimage(TIMESTAMP, BODY) == TIMESTAMP.encode("ascii") + b"." + BODY
    assert sign_request(secret=SECRET, timestamp=TIMESTAMP, body=BODY) == SIGNATURE
    verify_request(
        body=BODY,
        timestamp=TIMESTAMP,
        signature=SIGNATURE,
        now=int(TIMESTAMP),
    )


@pytest.mark.parametrize(
    ("timestamp", "signature", "body", "now", "code"),
    [
        (None, SIGNATURE, BODY, int(TIMESTAMP), "missing_authentication"),
        (TIMESTAMP, None, BODY, int(TIMESTAMP), "missing_authentication"),
        ("01786089600", SIGNATURE, BODY, int(TIMESTAMP), "invalid_timestamp"),
        (TIMESTAMP, "sha256=ABC", BODY, int(TIMESTAMP), "invalid_signature"),
        (str(int(TIMESTAMP) + 1), SIGNATURE, BODY, int(TIMESTAMP) + 1, "invalid_signature"),
        (TIMESTAMP, SIGNATURE, BODY, int(TIMESTAMP) + 301, "stale_timestamp"),
        (TIMESTAMP, SIGNATURE, BODY + b" ", int(TIMESTAMP), "invalid_signature"),
    ],
)
def test_rejects_invalid_or_modified_requests(
    monkeypatch, timestamp, signature, body, now, code
):
    _configure(monkeypatch)
    with pytest.raises(IntegrationAuthError) as caught:
        verify_request(
            body=body,
            timestamp=timestamp,
            signature=signature,
            now=now,
        )
    assert caught.value.code == code
