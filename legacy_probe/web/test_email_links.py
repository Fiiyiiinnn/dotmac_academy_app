"""Emailed links are absolute, on the verified tenant host, and https in production."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import APIRouter, FastAPI
from starlette.requests import Request

from app.web import email_links
from app.web.email_links import LinkOriginRefused, absolute_surface_link

TENANT = SimpleNamespace(id="tenant-a", slug="alpha")


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(
        email_links, "settings", SimpleNamespace(platform_root_domain="example.test", is_production=False)
    )

    @contextmanager
    def no_custom_domains():
        yield SimpleNamespace(scalar=lambda statement: None)

    monkeypatch.setattr(email_links, "resolver_session", no_custom_domains)


def _request(host="alpha.example.test", *, scheme="https", extra=(), tenant=TENANT) -> Request:
    app = FastAPI()
    router = APIRouter()

    @router.get("/reset", name="reset_form")
    def reset_form():  # pragma: no cover - routing target only
        return None

    app.include_router(router)
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/forgot",
            "query_string": b"",
            "root_path": "",
            "scheme": scheme,
            "server": ("internal", 80),
            "headers": [(b"host", host.encode()), *[(k.encode(), v.encode()) for k, v in extra]],
            "router": app.router,
            "app": app,
            "state": {"tenant": tenant},
        }
    )


def test_link_is_absolute_with_scheme_host_path_and_encoded_token():
    link = absolute_surface_link(_request(), "reset_form", token="a b&c=d")
    parts = urlsplit(link)
    assert (parts.scheme, parts.netloc, parts.path) == ("https", "alpha.example.test", "/reset")
    assert parse_qs(parts.query) == {"token": ["a b&c=d"]}


@pytest.mark.parametrize("host", ["alpha.example.test:1@evil.test", "evil.test", "beta.example.test", ""])
def test_a_host_that_is_not_the_tenants_never_reaches_a_link(host):
    with pytest.raises(LinkOriginRefused):
        absolute_surface_link(_request(host), "reset_form", token="t")


def test_forwarded_host_is_ignored():
    link = absolute_surface_link(_request(extra=[("x-forwarded-host", "evil.test")]), "reset_form", token="t")
    assert urlsplit(link).netloc == "alpha.example.test"


def test_a_verified_custom_domain_is_accepted(monkeypatch):
    @contextmanager
    def verified():
        yield SimpleNamespace(scalar=lambda statement: "learn.alpha.test")

    monkeypatch.setattr(email_links, "resolver_session", verified)
    link = absolute_surface_link(_request("learn.alpha.test"), "reset_form", token="t")
    assert urlsplit(link).netloc == "learn.alpha.test"


def test_production_links_are_https_and_cannot_be_downgraded(monkeypatch):
    monkeypatch.setattr(
        email_links, "settings", SimpleNamespace(platform_root_domain="example.test", is_production=True)
    )
    request = _request(scheme="http", extra=[("x-forwarded-proto", "http")])
    link = absolute_surface_link(request, "reset_form", token="t")
    assert link.startswith("https://alpha.example.test/reset?token=")


def test_non_production_keeps_the_request_scheme():
    link = absolute_surface_link(_request("alpha.example.test:8000", scheme="http"), "reset_form", token="t")
    assert link.startswith("http://alpha.example.test:8000/reset?token=")
