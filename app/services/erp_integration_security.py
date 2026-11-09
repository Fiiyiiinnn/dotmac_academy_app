"""Exact byte-level HMAC contract for ERP -> Academy requests."""

# Ported at new-composition genesis (ERP slice).

from __future__ import annotations

import hashlib
import hmac
import re
import time

from app.config import settings

_SIGNATURE_RE = re.compile(r"sha256=[0-9a-f]{64}\Z")
_TIMESTAMP_RE = re.compile(r"[0-9]+\Z")
MAX_SIGNED_BODY_BYTES = 16_384


class IntegrationAuthError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def signature_preimage(timestamp: str, body: bytes) -> bytes:
    """ASCII Unix seconds, one period byte, then the exact HTTP body bytes."""
    return timestamp.encode("ascii") + b"." + body


def sign_request(*, secret: str, timestamp: str, body: bytes) -> str:
    digest = hmac.new(
        secret.encode("utf-8"),
        signature_preimage(timestamp, body),
        hashlib.sha256,
    ).hexdigest()
    return f"sha256={digest}"


def verify_request(
    *,
    body: bytes,
    timestamp: str | None,
    signature: str | None,
    now: int | None = None,
) -> None:
    """Fail closed on missing, malformed, stale, or incorrectly signed input."""
    if not settings.erp_inbound_hmac_secret:
        raise IntegrationAuthError("integration_disabled")
    if len(body) > MAX_SIGNED_BODY_BYTES:
        raise IntegrationAuthError("request_too_large")
    if timestamp is None or signature is None:
        raise IntegrationAuthError("missing_authentication")
    if not _TIMESTAMP_RE.fullmatch(timestamp):
        raise IntegrationAuthError("invalid_timestamp")
    parsed = int(timestamp)
    if str(parsed) != timestamp:
        raise IntegrationAuthError("invalid_timestamp")
    current = int(time.time()) if now is None else now
    if abs(current - parsed) > settings.erp_inbound_hmac_max_skew_seconds:
        raise IntegrationAuthError("stale_timestamp")
    if not _SIGNATURE_RE.fullmatch(signature):
        raise IntegrationAuthError("invalid_signature")
    expected = sign_request(
        secret=settings.erp_inbound_hmac_secret,
        timestamp=timestamp,
        body=body,
    )
    if not hmac.compare_digest(expected, signature):
        raise IntegrationAuthError("invalid_signature")
