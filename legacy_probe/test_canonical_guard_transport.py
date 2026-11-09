"""Transport rejection order and API shape boundaries."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from starlette.requests import Request

from app.api import deps


def _request(tenant):
    return Request({"type": "http", "headers": [], "state": {"tenant": tenant}})


@pytest.mark.parametrize("credentials", [None, HTTPAuthorizationCredentials(scheme="Basic", credentials="ignored")])
def test_bearer_scheme_rejected_before_decode_or_session_query(monkeypatch, credentials):
    def forbidden(*args):
        raise AssertionError("invalid bearer scheme reached JWT decoder")

    monkeypatch.setattr(deps, "decode_access_token", forbidden)
    with pytest.raises(HTTPException) as caught:
        deps.require_user_auth(_request(SimpleNamespace(id=uuid4())), credentials, object())
    assert caught.value.status_code == 401


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {"tenant_id": "wrong", "sub": "invalid"},
        {"tenant_id": "matching"},
        {"tenant_id": "matching", "sub": "invalid"},
    ],
)
def test_claim_validation_precedes_session_query(monkeypatch, payload):
    tid = uuid4()
    if payload is not None and payload.get("tenant_id") == "matching":
        payload = {**payload, "tenant_id": str(tid)}
    monkeypatch.setattr(deps, "decode_access_token", lambda token: payload)
    with pytest.raises(HTTPException) as caught:
        deps.require_user_auth(
            _request(SimpleNamespace(id=tid)),
            HTTPAuthorizationCredentials(scheme="Bearer", credentials="raw"),
            object(),
        )
    assert caught.value.status_code == 401


def test_tenant_missing_precedes_bearer_decode_and_query(monkeypatch):
    def forbidden(*args):
        raise AssertionError("missing tenant reached decoder")

    monkeypatch.setattr(deps, "decode_access_token", forbidden)
    with pytest.raises(HTTPException) as caught:
        deps.require_user_auth(_request(None), None, object())
    assert caught.value.status_code == 404 and caught.value.detail == "Tenant not found"
