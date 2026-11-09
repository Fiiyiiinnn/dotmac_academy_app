"""Route boundary compatibility; database behavior lives in genuine PG tests."""

from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.responses import HTMLResponse
from starlette.requests import Request

from app.api import auth as api
from app.web import auth as web


def request(hx=False, https=False):
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/login",
            "scheme": "https" if https else "http",
            "server": ("academy.test", 443),
            "query_string": b"",
            "headers": [(b"hx-request", b"true")] if hx else [],
        }
    )


def test_api_bad_credentials_returns_normal_401_and_success_bearer(monkeypatch):
    operation = Mock(return_value=None)
    monkeypatch.setattr(api.web_auth, "sign_in", operation)
    tenant, db = SimpleNamespace(id=uuid4()), object()
    payload = api.LoginRequest(email="user@a.edu", password="chosen9")
    response = api.login(payload, db, tenant)
    assert response.status_code == 401 and response.body == b'{"detail":"Invalid credentials"}'
    operation.assert_called_once_with(db, tenant_id=tenant.id, email=payload.email, password=payload.password)
    operation.return_value = SimpleNamespace(session_token="response-only")
    response = api.login(payload, db, tenant)
    assert response.access_token == "response-only" and response.token_type == "bearer"


@pytest.mark.parametrize("hx", [False, True])
def test_web_invalid_response_contract(monkeypatch, hx):
    monkeypatch.setattr(web, "require_tenant", lambda request: SimpleNamespace(id=uuid4()))
    monkeypatch.setattr(web.web_auth, "sign_in", lambda *args, **kwargs: None)
    renderer = Mock(return_value=HTMLResponse("Invalid credentials", status_code=401))
    monkeypatch.setattr(web.templates, "TemplateResponse", renderer)
    response = web.login(request(hx), "user@a.edu", "bad", object())
    assert response.status_code == (200 if hx else 401)
    if hx:
        assert response.body == b"Invalid credentials"
        assert not renderer.called
    else:
        assert renderer.call_args.kwargs["status_code"] == 401


@pytest.mark.parametrize(
    "slugs,target", [({"instructor"}, "/instructor"), ({"instructor", "admin"}, "/"), ({"student"}, "/")]
)
def test_web_session_precedes_roles_and_cookie_contract(monkeypatch, slugs, target):
    calls = []
    tid, pid = uuid4(), uuid4()
    monkeypatch.setattr(web, "require_tenant", lambda request: SimpleNamespace(id=tid))

    def operation(db, **kwargs):
        calls.append("session")
        assert kwargs["tenant_id"] == tid
        return SimpleNamespace(account=SimpleNamespace(id=pid), session_token="response-only")

    def access(db, *, tenant_id, person_id):
        assert calls == ["session"] and (tenant_id, person_id) == (tid, pid)
        calls.append("access")
        return SimpleNamespace(can_teach=bool(slugs & {"instructor", "admin"}), is_admin="admin" in slugs)

    monkeypatch.setattr(web.web_auth, "sign_in", operation)
    monkeypatch.setattr(web, "facilitator_access", access)
    response = web.login(request(False, True), "user@a.edu", "chosen9", object())
    assert calls == ["session", "access"]
    assert response.status_code == 303 and response.headers["location"] == target
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Secure" in cookie


def test_no_arbitrary_uuid_session_issuer():
    assert not hasattr(api.web_auth, "start_session")
    assert not hasattr(api.web_auth, "authenticate")
