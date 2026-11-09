"""Hosted regression coverage for the bounded, reviewed console document."""

from __future__ import annotations

import gzip
import hashlib
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from dotmac_kernel.facet_principal import FacetPrincipal, record_facet_principal
from dotmac_kernel.web_surfaces import BrowserSecurityPlane, WebRouteRef
from fastapi import HTTPException
from starlette.requests import Request

from app.assembly import assembly
from app.web import console_document as document
from app.web import labs
from tests.issue2_wave3_support import current_account

assert current_account  # Imported pytest fixture.


class Stream(httpx.AsyncByteStream):
    def __init__(self, chunks, *, fail=False):
        self.chunks = chunks
        self.fail = fail
        self.closed = False
        self.reads = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk
        if self.fail:
            raise httpx.ReadError("broken upstream")

    async def aclose(self):
        self.closed = True


class Client:
    def __init__(self, upstream):
        self.upstream = upstream
        self.closed = False
        self.request = None

    def build_request(self, *args, **kwargs):
        self.request = httpx.Request(*args, **kwargs)
        return self.request

    async def send(self, request, *, stream):
        assert stream
        return self.upstream

    async def aclose(self):
        self.closed = True


def request_for(path="/labs/instances/test/console/linux", *, method="GET", query=b"a=1&a=2"):
    return Request(
        {
            "type": "http",
            "method": method,
            "scheme": "https",
            "path": path,
            "query_string": query,
            "headers": [(b"host", b"academy.localhost"), (b"range", b"bytes=0-1")],
            "path_params": {"instance_id": "test", "node": "linux"},
        }
    )


def synthetic_review(monkeypatch, value):
    # The transport tests use a small synthetic reviewed document; production's
    # immutable reviewed identity is asserted separately below.
    monkeypatch.setattr(document, "TTYD_DOCUMENT_BYTES", len(value))
    monkeypatch.setattr(document, "TTYD_DOCUMENT_SHA256", hashlib.sha256(value).hexdigest())


@pytest.mark.asyncio
async def test_proxy_buffers_exact_bytes_and_closes_before_return(monkeypatch):
    reviewed = gzip.decompress((Path(__file__).parent / "fixtures/console/ttyd-1.7.4.index.html.gz").read_bytes())
    scripts = re.findall(rb"<script[^>]*>(.*?)</script>", reviewed, re.DOTALL)
    assert len(scripts) == 1 and len(scripts[0]) == 695225
    assert hashlib.sha256(scripts[0]).hexdigest() == "f0b422e81e9b3cd470652c7c0126f9a4110fdf6ab8b692366633409f94b7a22d"
    stream = Stream([reviewed[:8], reviewed[8:]])
    client = Client(httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, stream=stream))
    monkeypatch.setattr(labs, "_new_upstream_client", lambda: client)
    response = await labs._proxy_http(request_for(), "http://localhost:9000/base/", reviewed_document=True)
    assert response.status_code == 200 and response.body == reviewed
    assert client.closed and stream.closed
    assert "range" not in client.request.headers
    assert client.request.headers["accept-encoding"] == "identity"
    assert client.request.url.query == b"a=1&a=2"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["digest", "short", "bound", "read-error", "content-type", "status"])
async def test_unreviewed_document_refuses_before_response_and_closes(monkeypatch, case):
    synthetic_review(monkeypatch, b"reviewed")
    chunks = {
        "digest": [b"changed!"],
        "short": [b"short"],
        "bound": [b"x" * 9, b"must not be read"],
    }.get(case, [b"reviewed"])
    stream = Stream(chunks, fail=case == "read-error")
    upstream = httpx.Response(
        206 if case == "status" else 200,
        headers={"content-type": "application/json" if case == "content-type" else "text/html"},
        stream=stream,
    )
    client = Client(upstream)
    monkeypatch.setattr(labs, "_new_upstream_client", lambda: client)
    with pytest.raises(HTTPException) as error:
        await labs._proxy_http(request_for(), "http://localhost:9000/base/", reviewed_document=True)
    assert error.value.status_code == 502
    assert stream.closed and client.closed
    if case == "bound":
        assert stream.reads == 1


def test_only_bounded_console_declares_reviewed_script():
    assert len(assembly.browser_document_policies) == 1
    policy = assembly.browser_document_policies[0]
    assert policy.facet == "staff_admin"
    assert policy.route == WebRouteRef("academy", "legacy", "console")
    assert policy.inline_scripts[0].sha256 == "8LQi6B6bPNRwZSx8ASb5pBEP32q4tpI2ZjNAn5S3oi0="
    assert document.TTYD_DOCUMENT_BYTES == 701420
    assert document.TTYD_DOCUMENT_SHA256 == "065fa93ecafdd9e5c8bb687b9c59e8aa3c292518b20b4e896abb858b2ae67fe8"


@pytest.mark.parametrize("mismatch", [None, "party", "tenant", "facet", "absent", "plane"])
def test_current_account_matches_facet_party_and_tenant(mismatch):
    tenant, party = uuid4(), uuid4()
    request = request_for()
    person = SimpleNamespace(id=party, tenant_id=tenant)
    principal = FacetPrincipal("staff_admin", BrowserSecurityPlane.TENANT, party, object(), tenant)
    if mismatch == "party":
        principal = replace(principal, subject_id=uuid4())
    elif mismatch == "tenant":
        principal = replace(principal, tenant_id=uuid4())
    elif mismatch == "facet":
        principal = replace(principal, facet="other")
    elif mismatch == "plane":
        principal = replace(principal, security_plane=BrowserSecurityPlane.PLATFORM)
    if mismatch != "absent":
        record_facet_principal(request, principal)
    if mismatch:
        with pytest.raises(HTTPException) as error:
            document.require_console_principal(request, person, tenant)
        assert error.value.status_code == 403
    else:
        document.require_console_principal(request, person, tenant)


@pytest.mark.asyncio
async def test_ttyd_slash_redirect_authorizes_before_use_and_preserves_query(monkeypatch):
    tenant = SimpleNamespace(id=uuid4())
    person = SimpleNamespace(id=uuid4(), tenant_id=tenant.id)
    request = request_for("/labs/instances/test/console/linux/")
    record_facet_principal(
        request, FacetPrincipal("staff_admin", BrowserSecurityPlane.TENANT, person.id, object(), tenant.id)
    )
    calls = []
    monkeypatch.setattr(labs, "require_tenant", lambda request: tenant)

    def authorize(*args):
        calls.append("authorize")
        return f"http://{labs.settings.lab_console_host}:9000/"

    monkeypatch.setattr(labs, "_authorize_console", authorize)
    monkeypatch.setattr(labs, "_note_console_use", lambda *args: pytest.fail("redirect must not record use"))
    monkeypatch.setattr(labs, "_proxy_http", lambda *args, **kwargs: pytest.fail("redirect must not proxy"))
    response = await labs.console_subpath(uuid4(), "linux", "", request, person, object())
    assert response.status_code == 308 and calls == ["authorize"]
    assert response.headers["location"] == "/labs/instances/test/console/linux?a=1&a=2"


@pytest.mark.asyncio
async def test_slash_redirect_cannot_bypass_authorization(monkeypatch):
    monkeypatch.setattr(labs, "require_tenant", lambda request: SimpleNamespace(id=uuid4()))

    def deny(*args):
        raise HTTPException(status_code=404)

    monkeypatch.setattr(labs, "_authorize_console", deny)
    with pytest.raises(HTTPException) as error:
        await labs.console_subpath(uuid4(), "linux", "", request_for(), object(), object())
    assert error.value.status_code == 404


def test_actual_academy_composition_binds_only_bounded_get():
    from app.kernel_runtime import create_academy_app

    app = create_academy_app()
    effective = []
    for route in app.routes:
        contexts = getattr(route, "effective_route_contexts", None)
        effective.extend(contexts() if callable(contexts) else (route,))
    routes = [route for route in effective if getattr(route, "endpoint", None) is labs.console]
    assert len(routes) == 1
    assert routes[0].path == "/labs/instances/{instance_id}/console/{node}"
    assert routes[0].methods == {"GET"}
    subpaths = [route for route in effective if getattr(route, "endpoint", None) is labs.console_subpath]
    assert len(subpaths) == 1 and ":path}" in subpaths[0].path


def test_actual_composition_refuses_hash_binding_to_catchall():
    from dotmac_kernel.web_surfaces import WebSurfaceError

    from app.kernel_runtime import create_academy_app

    policy = replace(assembly.browser_document_policies[0], route=WebRouteRef("academy", "legacy", "console_subpath"))
    with pytest.raises(WebSurfaceError):
        create_academy_app(replace(assembly, browser_document_policies=(policy,)))


@pytest.mark.asyncio
async def test_no_slash_webfig_redirects_without_proxy_or_document_grant(monkeypatch):
    tenant = SimpleNamespace(id=uuid4())
    person = SimpleNamespace(id=uuid4(), tenant_id=tenant.id)
    request = request_for()
    record_facet_principal(
        request, FacetPrincipal("staff_admin", BrowserSecurityPlane.TENANT, person.id, object(), tenant.id)
    )
    monkeypatch.setattr(labs, "require_tenant", lambda request: tenant)
    monkeypatch.setattr(labs, "_authorize_console", lambda *args: "http://192.0.2.1/")
    monkeypatch.setattr(labs, "_note_console_use", lambda *args: None)
    monkeypatch.setattr(labs, "_proxy_http", lambda *args, **kwargs: pytest.fail("WebFig must redirect"))
    response = await labs.console(uuid4(), "router", request, person, object())
    assert response.status_code == 308
    assert response.headers["location"] == "/labs/instances/test/console/linux/?a=1&a=2"


@pytest.mark.asyncio
async def test_no_slash_ttyd_opens_once_and_requires_review(monkeypatch):
    tenant = SimpleNamespace(id=uuid4())
    person = SimpleNamespace(id=uuid4(), tenant_id=tenant.id)
    request = request_for()
    record_facet_principal(
        request, FacetPrincipal("staff_admin", BrowserSecurityPlane.TENANT, person.id, object(), tenant.id)
    )
    calls = []
    monkeypatch.setattr(labs, "require_tenant", lambda request: tenant)
    monkeypatch.setattr(labs, "_authorize_console", lambda *args: f"http://{labs.settings.lab_console_host}:9000/")
    monkeypatch.setattr(labs, "_note_console_use", lambda *args: calls.append("use"))
    instance = uuid4()

    async def proxy(actual_request, target, *, webfig=False, reviewed_document=False):
        assert actual_request is request and reviewed_document and not webfig
        assert target == f"http://{labs.settings.lab_console_host}:9000/labs/instances/{instance}/console/linux/"
        calls.append("proxy")
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(labs, "_proxy_http", proxy)
    response = await labs.console(instance, "linux", request, person, object())
    assert response.status_code == 200 and calls == ["use", "proxy"]


def test_actual_tenant_owner_and_entitlement_gates(genesis_database, monkeypatch):
    from sqlalchemy.orm import Session

    from tests.conftest import _scoped, seed_lab_scenario

    admin, _, _ = genesis_database
    tenant, party, _, _, _, instance = seed_lab_scenario(admin)
    with Session(admin) as db:
        _scoped(db, tenant)
        for request_tenant, request_party, status in [(uuid4(), party, 404), (tenant, uuid4(), 403)]:
            with pytest.raises(HTTPException) as error:
                labs._authorize_console(
                    db, SimpleNamespace(id=request_tenant), SimpleNamespace(id=request_party), instance, "linux"
                )
            assert error.value.status_code == status

        def deny_entitlement(*args, **kwargs):
            raise HTTPException(status_code=403, detail="course closed")

        monkeypatch.setattr(labs, "require_course_open", deny_entitlement)
        with pytest.raises(HTTPException) as error:
            labs._authorize_console(db, SimpleNamespace(id=tenant), SimpleNamespace(id=party), instance, "linux")
        assert error.value.status_code == 403 and error.value.detail == "course closed"


@pytest.mark.asyncio
async def test_reviewed_response_preserves_each_enforcing_csp(monkeypatch):
    synthetic_review(monkeypatch, b"reviewed")
    upstream = httpx.Response(
        200,
        headers=[
            ("content-type", "text/html"),
            ("content-security-policy", "script-src 'none'"),
            ("content-security-policy", "connect-src 'none'"),
            ("set-cookie", "device=untrusted"),
        ],
        stream=Stream([b"reviewed"]),
    )
    monkeypatch.setattr(labs, "_new_upstream_client", lambda: Client(upstream))
    response = await labs._proxy_http(request_for(), "http://localhost/base/", reviewed_document=True)
    assert response.headers.getlist("content-security-policy") == ["script-src 'none'", "connect-src 'none'"]
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["gzip", "br", "gzip, identity"])
async def test_encoded_document_refuses_without_read_and_closes(monkeypatch, encoding):
    stream = Stream([b"must not be read"])
    upstream = httpx.Response(200, headers={"content-type": "text/html", "content-encoding": encoding}, stream=stream)
    client = Client(upstream)
    monkeypatch.setattr(labs, "_new_upstream_client", lambda: client)
    with pytest.raises(HTTPException) as error:
        await labs._proxy_http(request_for(), "http://localhost/base/", reviewed_document=True)
    assert error.value.status_code == 502
    assert stream.reads == 0 and stream.closed and client.closed


@pytest.mark.parametrize(
    ("scheme", "host", "origins", "allowed"),
    [
        ("ws", "academy.localhost", ["http://academy.localhost"], True),
        ("wss", "academy.localhost:443", ["https://academy.localhost"], True),
        ("ws", "academy.localhost", ["http://academy.localhost:80"], True),
        ("wss", "academy.localhost", ["http://academy.localhost"], False),
        ("ws", "academy.localhost", [], False),
        ("ws", "academy.localhost", ["null"], False),
        ("ws", "academy.localhost", ["http://other.localhost"], False),
        ("ws", "academy.localhost", ["http://academy.localhost", "http://academy.localhost"], False),
        ("ws", "academy.localhost", ["http://user@academy.localhost"], False),
        ("ws", "academy.localhost", ["http://academy.localhost/path"], False),
        ("ws", "academy.localhost", ["http://academy.localhost:bad"], False),
        ("ws", "academy.localhost", ["http://academy.localhost:81"], False),
        ("ws", "academy.localhost", ["http://academy.localhost#fragment"], False),
        ("ws", "academy.localhost", ["http://academy.localhost?"], False),
        ("ws", "academy.localhost", ["http://academy.localhost:"], False),
    ],
)
def test_console_websocket_origin(scheme, host, origins, allowed):
    from starlette.websockets import WebSocket

    socket = WebSocket(
        {
            "type": "websocket",
            "scheme": scheme,
            "headers": [(b"host", host.encode()), *[(b"origin", value.encode()) for value in origins]],
        },
        None,
        None,
    )
    assert labs._console_ws_same_origin(socket) is allowed


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", [None, "http://other.localhost"])
async def test_bad_origin_closes_before_tenant_database_or_upstream(monkeypatch, origin):
    class Socket:
        scope = {"scheme": "ws"}
        headers = httpx.Headers({"host": "academy.localhost"})
        closed = None

        async def close(self, *, code):
            self.closed = code

    # Starlette's getlist accessor, rather than httpx's differently named helper.
    from starlette.datastructures import Headers

    socket = Socket()
    socket.headers = Headers({"host": "academy.localhost", **({"origin": origin} if origin else {})})
    monkeypatch.setattr(labs._tenant_resolver, "_resolve", lambda *args: pytest.fail("origin precedes tenant lookup"))
    monkeypatch.setattr(labs, "resolve_database_runtime", lambda: pytest.fail("origin precedes database lookup"))
    monkeypatch.setattr(labs, "_proxy_ws", lambda *args: pytest.fail("origin precedes upstream connect"))
    await labs.console_ws(socket, uuid4(), "linux")
    assert socket.closed == 1008


_COMPOSED_CONSOLE = r"""
import gzip
import os
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from app.kernel_runtime import create_academy_app
from app.web import labs

reviewed = gzip.decompress(Path("tests/fixtures/console/ttyd-1.7.4.index.html.gz").read_bytes())
state = {"body": reviewed, "policies": []}
closed = []

class UpstreamStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield state["body"]

    async def aclose(self):
        closed.append(True)

def upstream(request):
    return httpx.Response(200, headers=[("content-type", "text/html"), *state["policies"]], stream=UpstreamStream())

labs._new_upstream_client = lambda: httpx.AsyncClient(transport=httpx.MockTransport(upstream))
app = create_academy_app()
expected_hash = "8LQi6B6bPNRwZSx8ASb5pBEP32q4tpI2ZjNAn5S3oi0="
with TestClient(app, base_url=os.environ["CONSOLE_TEST_ORIGIN"], follow_redirects=False) as client:
    client.cookies.set("session", os.environ["CONSOLE_TEST_SESSION"])
    path = os.environ["CONSOLE_TEST_PATH"]
    response = client.get(path)
    assert response.status_code == 200, response.text[:200]
    assert response.content == reviewed
    assert expected_hash in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"
    assert len(closed) == 1
    state["body"] = reviewed[:-1] + bytes([reviewed[-1] ^ 1])
    drift = client.get(path)
    assert drift.status_code == 502, drift.text[:200]
    assert expected_hash not in drift.headers["content-security-policy"]
    assert len(closed) == 2
    state["body"] = reviewed
    state["policies"] = [("content-security-policy", "script-src 'none'"),
                         ("content-security-policy", "connect-src 'none'")]
    strict = client.get(path)
    assert strict.status_code == 200 and strict.content == reviewed
    policies = strict.headers.get_list("content-security-policy")
    assert "script-src 'none'" in policies and "connect-src 'none'" in policies
    assert expected_hash not in " ".join(policies)
    assert len(closed) == 3
print("COMPOSED-CONSOLE-OK")
"""


def test_genuine_composed_session_owner_reviewed_document_and_drift(current_account):
    """Only upstream transport is fake; tenant, facet, session and gates are real."""
    import os
    import subprocess
    import sys

    from legacy_probe.web.test_console_ws import _seed_instance
    from tests.issue2_wave3_support import grant_std

    a = current_account
    grant_std(a.admin, tenant_id=a.tid, party_id=a.pid)
    a.admin.commit()
    instance = _seed_instance(a.admin, a.tenant, a.pid)
    env = {
        **os.environ,
        "DATABASE_URL": a.app_engine.url.render_as_string(hide_password=False),
        "PLATFORM_DATABASE_URL": a.platform_engine.url.render_as_string(hide_password=False),
        "ENVIRONMENT": "dev",
        "CSRF_ENABLED": "true",
        "RATE_LIMIT_ENABLED": "false",
        "CONSOLE_TEST_ORIGIN": f"http://{a.tenant.slug}.localhost",
        "CONSOLE_TEST_SESSION": a.token,
        "CONSOLE_TEST_PATH": f"/labs/instances/{instance.id}/console/r1",
    }
    result = subprocess.run(  # noqa: S603 - fixed interpreter/script; isolated hosted genesis fixture
        [sys.executable, "-c", _COMPOSED_CONSOLE],
        cwd=Path(__file__).parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    assert "COMPOSED-CONSOLE-OK" in result.stdout


def test_raw_forwarded_proto_cannot_supply_websocket_origin_proof():
    from starlette.websockets import WebSocket

    socket = WebSocket(
        {
            "type": "websocket",
            "scheme": "ws",
            "headers": [
                (b"host", b"academy.localhost"),
                (b"origin", b"https://academy.localhost"),
                (b"x-forwarded-proto", b"https"),
            ],
        },
        None,
        None,
    )
    assert not labs._console_ws_same_origin(socket)
