"""RouterOS WebFig through the per-lab console proxy (POST /jsproxy).

The upstream is a fake (httpx.MockTransport); no router is contacted. Checks the
method allow-list, owner/tenant gate for POST, SSRF/path escapes, body cap,
header and cookie confinement, redirect rewriting and the narrow CSRF bridge.
"""

from __future__ import annotations

import json
import zlib
from pathlib import Path

import httpx
import pytest
from dotmac_kernel.middleware.csrf import CSRFMiddleware, require_csrf
from fastapi import Depends, FastAPI, Request, Response
from fastapi.testclient import TestClient

from app.config import settings
from app.models.lab import LabInstance
from app.web import labs
from app.web.console_proxy import (
    WebFigRootRewriter,
    console_upstream_timeout,
    safe_mgmt_address,
    should_rewrite_webfig_body,
)
from legacy_probe.web.test_console_proxy import _entitled_activity, _login, _make_person

pytestmark = pytest.mark.private_fixture_data

_WEBFIG_FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "webfig"


def _pinned_response(path):
    manifest = json.loads((Path(__file__).resolve().parents[2] / "app" / "web" / "webfig_assets_v1.json").read_text())
    spec = manifest["assets"].get(path)
    if spec is None:
        return httpx.Response(200, content=b"ok")
    wire = (_WEBFIG_FIXTURES / (spec["sha256"] + ".gz")).read_bytes()
    decoder = zlib.decompressobj(31)
    entity = decoder.decompress(wire, spec["bytes"] + 1)
    assert decoder.eof and not decoder.unconsumed_tail and not decoder.unused_data
    assert len(entity) == spec["bytes"]
    return httpx.Response(200, content=entity, headers={"content-type": spec["content_type"]})


MGMT = "10.123.0.10"  # RFC 1918 fake device; private on every supported Python
ORIGIN = {"Origin": "http://alpha.localhost"}


def _seed_ros(admin_session, tenant, person_id, mgmt=MGMT) -> LabInstance:
    li = LabInstance(
        tenant_id=tenant.id,
        activity_id=_entitled_activity(admin_session, tenant, person_id),
        person_id=person_id,
        instance_name="dal-ros",
        seed={"o": 5},
        status="active",
        runtime_presence="present",
        consoles={"r1": {"kind": "vr-ros", "mgmt": mgmt}},
    )
    admin_session.add(li)
    admin_session.commit()
    admin_session.refresh(li)
    return li


@pytest.fixture
def upstream(monkeypatch):
    """Fake RouterOS: records requests, answers via ``upstream.respond``."""
    calls: list[httpx.Request] = []

    class Fake:
        respond = staticmethod(lambda req: _pinned_response(req.url.path))

    fake = Fake()

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return fake.respond(req)

    class _Transport(httpx.AsyncBaseTransport):
        # MockTransport pre-reads the body, which breaks the proxy's aiter_raw();
        # hand back an unconsumed byte stream like a real connection does.
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            resp = handler(request)
            return httpx.Response(
                resp.status_code, headers=resp.headers.raw, stream=httpx.ByteStream(resp.content), request=request
            )

    monkeypatch.setattr(
        "app.web.labs._new_upstream_client",
        lambda: httpx.AsyncClient(transport=_Transport(), follow_redirects=False),
    )
    fake.calls = calls
    return fake


@pytest.fixture
def owned(app_client, admin_session, tenant_a):
    p = _make_person(admin_session, tenant_a, "ros-owner@a.edu")
    li = _seed_ros(admin_session, tenant_a, p.id)
    h = _login(app_client, "ros-owner@a.edu")
    app_client.get("/", headers=h)  # make sure the csrf_token cookie exists
    assert app_client.cookies.get("csrf_token")
    return li, h


def _url(li, path="jsproxy"):
    return f"/labs/instances/{li.id}/console/r1/{path}"


def test_post_jsproxy_passes_through(app_client, owned, upstream):
    li, h = owned
    upstream.respond = lambda req: httpx.Response(
        200, content=b"\x01reply", headers={"content-type": "application/octet-stream"}
    )
    r = app_client.post(
        _url(li), headers={**h, **ORIGIN, "content-type": "text/plain"}, content=b"\x00enc-session-blob"
    )
    assert r.status_code == 200 and r.content == b"\x01reply"
    (call,) = upstream.calls
    assert call.method == "POST" and str(call.url) == f"http://{MGMT}/jsproxy"
    assert call.content == b"\x00enc-session-blob"
    assert call.headers["content-type"] == "text/plain"


def test_get_and_head_still_work(app_client, owned, upstream):
    li, h = owned
    get_response = app_client.get(_url(li, "webfig/"), headers=h)
    head_response = app_client.head(_url(li, "webfig/"), headers=h)
    assert get_response.status_code == head_response.status_code == 200
    assert get_response.content
    assert head_response.content == b""
    assert int(get_response.headers["content-length"]) == len(get_response.content)
    assert head_response.headers["content-length"] == get_response.headers["content-length"]
    # HEAD verifies the full pinned entity with GET before returning only the
    # headers for the transformed representation to the learner.
    assert [c.method for c in upstream.calls] == ["GET", "GET"]
    assert all(str(call.url) == f"http://{MGMT}/webfig/" for call in upstream.calls)


@pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH"])
def test_other_methods_rejected(app_client, owned, upstream, method):
    li, h = owned
    tok = app_client.cookies.get("csrf_token")
    r = app_client.request(method, _url(li), headers={**h, "x-csrf-token": tok})
    assert r.status_code == 405 and not upstream.calls


def test_other_user_forbidden_on_post(app_client, admin_session, tenant_a, upstream):
    owner = _make_person(admin_session, tenant_a, "ros-o2@a.edu")
    _make_person(admin_session, tenant_a, "ros-intruder@a.edu")
    li = _seed_ros(admin_session, tenant_a, owner.id)
    h = _login(app_client, "ros-intruder@a.edu")
    app_client.get("/", headers=h)
    r = app_client.post(_url(li), headers={**h, **ORIGIN}, content=b"x")
    assert r.status_code == 403 and not upstream.calls


def test_cross_tenant_not_found_on_post(app_client, admin_session, tenant_a, tenant_b, upstream):
    _make_person(admin_session, tenant_a, "ros-o3@a.edu")
    pb = _make_person(admin_session, tenant_b, "o@b.edu")
    li = _seed_ros(admin_session, tenant_b, pb.id)
    h = _login(app_client, "ros-o3@a.edu")
    app_client.get("/", headers=h)
    r = app_client.post(_url(li), headers={**h, **ORIGIN}, content=b"x")
    assert r.status_code == 404 and not upstream.calls


def test_unauthenticated_post_never_proxied(app_client, admin_session, tenant_a, upstream):
    p = _make_person(admin_session, tenant_a, "ros-o4@a.edu")
    li = _seed_ros(admin_session, tenant_a, p.id)
    r = app_client.post(_url(li), headers={"Host": "alpha.localhost"}, content=b"x", follow_redirects=False)
    assert r.status_code in (303, 401, 403) and not upstream.calls


@pytest.mark.parametrize(
    "mgmt", ["localhost", "127.0.0.1", "169.254.169.254", "8.8.8.8", f"{MGMT}:8080", f"u@{MGMT}", "::1", f"{MGMT}/x"]
)
def test_unsafe_mgmt_address_has_no_target(app_client, admin_session, tenant_a, upstream, mgmt):
    p = _make_person(admin_session, tenant_a, f"ros-m{abs(hash(mgmt))}@a.edu")
    li = _seed_ros(admin_session, tenant_a, p.id, mgmt=mgmt)
    h = _login(app_client, f"ros-m{abs(hash(mgmt))}@a.edu")
    app_client.get("/", headers=h)
    r = app_client.post(_url(li), headers={**h, **ORIGIN}, content=b"x")
    assert r.status_code == 404 and not upstream.calls


@pytest.mark.parametrize("path", ["/evil.test/x", "http://evil.test/", "a%5Cb", "a%00b", "a//b"])
def test_path_escapes_rejected(app_client, owned, upstream, path):
    li, h = owned
    r = app_client.get(_url(li, path), headers=h)
    assert r.status_code in (400, 404) and not upstream.calls
    r = app_client.post(_url(li, path), headers={**h, **ORIGIN}, content=b"x")
    assert r.status_code in (400, 404) and not upstream.calls


@pytest.mark.parametrize("path", ["../x", "a/../b", "./x", "a/./b", "x/..", "/x", "a//b", "a\\b", "a\x00b", "a\nb"])
def test_safe_subpath_rejects_escapes(path):
    # httpx normalises dot-segments before sending, so exercise the validator
    # directly for the traversal forms a raw client could still deliver.
    from fastapi import HTTPException

    from app.web.console_proxy import safe_subpath

    with pytest.raises(HTTPException) as exc:
        safe_subpath(path)
    assert exc.value.status_code == 400


def test_safe_subpath_encodes_delimiters():
    from app.web.console_proxy import safe_subpath

    assert safe_subpath("a?b#c d") == "a%3Fb%23c%20d"
    assert safe_subpath("jsproxy/get") == "jsproxy/get"


def test_bridge_path_matcher_is_narrow():
    from app.kernel_runtime import _is_console_post_path as ok

    u = "12345678-1234-1234-1234-123456789abc"
    assert ok(f"/labs/instances/{u}/console/r1/jsproxy")
    assert not ok(f"/labs/instances/{u}/console/r1")
    assert not ok(f"/labs/instances/{u}/check")
    assert not ok(f"/labs/instances/{u}/console/r1/../../check")
    assert not ok("/login")


def test_encoded_query_chars_stay_in_path(app_client, owned, upstream):
    li, h = owned
    app_client.get(_url(li, "a%3Fb%23c"), headers=h)
    (call,) = upstream.calls
    assert call.url.host == MGMT and call.url.raw_path == b"/a%3Fb%23c"


def test_body_cap_content_length(app_client, owned, upstream, monkeypatch):
    li, h = owned
    monkeypatch.setattr("app.web.labs.settings.lab_console_max_body_bytes", 16)
    r = app_client.post(_url(li), headers={**h, **ORIGIN}, content=b"x" * 17)
    assert r.status_code == 413 and not upstream.calls
    assert app_client.post(_url(li), headers={**h, **ORIGIN}, content=b"x" * 16).status_code == 200


def test_body_cap_chunked(app_client, owned, upstream, monkeypatch):
    li, h = owned
    monkeypatch.setattr("app.web.labs.settings.lab_console_max_body_bytes", 16)
    r = app_client.post(_url(li), headers={**h, **ORIGIN}, content=(b"x" * 8 for _ in range(3)))
    assert r.status_code == 413 and not upstream.calls


def test_learner_credentials_not_forwarded_and_device_cookie_confined(app_client, owned, upstream):
    li, h = owned
    sess = app_client.cookies.get("session")
    assert sess
    upstream.respond = lambda req: httpx.Response(
        200,
        headers=[
            ("set-cookie", "sid=abc123; Path=/; Domain=evil.test; Max-Age=60"),
            ("set-cookie", "session=hijack; Path=/"),
            ("set-cookie", "bad name=1"),
        ],
    )
    app_client.cookies.set("wf_sid", "dev1")
    r = app_client.post(
        _url(li),
        headers={
            **h,
            **ORIGIN,
            "authorization": "Bearer learner-secret",
            "x-csrf-token": app_client.cookies["csrf_token"],
            "referer": "http://alpha.localhost/labs",
            "x-forwarded-for": "198.51.100.7",
        },
        content=b"x",
    )
    assert r.status_code == 200
    (call,) = upstream.calls
    sent = {k.lower(): v for k, v in call.headers.items()}
    assert sent.get("cookie") == "sid=dev1"  # device-namespaced cookie only
    for banned in ("authorization", "x-csrf-token", "origin", "referer", "x-forwarded-for"):
        assert banned not in sent
    assert sess not in str(call.headers)
    cookies = r.headers.get_list("set-cookie")
    wf = [c for c in cookies if c.startswith("wf_")]
    pre = f"Path=/labs/instances/{li.id}/console/r1/; HttpOnly; SameSite=Strict"
    # A device cannot overwrite Academy's own "session" cookie: it is namespaced.
    assert wf == [f"wf_sid=abc123; {pre}; Max-Age=60", f"wf_session=hijack; {pre}"]
    assert not any(c.startswith(("session=", "bad")) or "evil.test" in c for c in cookies)


def test_no_slash_console_url_redirects_for_routeros(app_client, owned, upstream):
    li, h = owned
    r = app_client.get(f"/labs/instances/{li.id}/console/r1?x=1", headers=h, follow_redirects=False)
    assert r.status_code == 308
    assert r.headers["location"] == f"/labs/instances/{li.id}/console/r1/?x=1"
    assert not upstream.calls
    assert app_client.get(f"/labs/instances/{li.id}/console/r1/", headers=h).status_code == 200


def test_console_link_template_has_trailing_slash():
    src = open("templates/labs/_status.html").read()
    assert 'console/{{ node }}/"' in src


def test_device_redirects_stay_inside_prefix(app_client, owned, upstream):
    li, h = owned
    pre = f"/labs/instances/{li.id}/console/r1"
    upstream.respond = lambda req: httpx.Response(302, headers={"location": "/webfig/"})
    r = app_client.get(_url(li, "x"), headers=h, follow_redirects=False)
    assert r.headers["location"] == f"{pre}/webfig/"
    upstream.respond = lambda req: httpx.Response(302, headers={"location": f"http://{MGMT}/webfig/?a=1"})
    r = app_client.get(_url(li, "x"), headers=h, follow_redirects=False)
    assert r.headers["location"] == f"{pre}/webfig/?a=1"
    upstream.respond = lambda req: httpx.Response(302, headers={"location": "http://evil.test/"})
    r = app_client.get(_url(li, "x"), headers=h, follow_redirects=False)
    assert "location" not in r.headers
    upstream.respond = lambda req: httpx.Response(302, headers={"location": "../../../../login?next=1"})
    r = app_client.get(_url(li, "nested/x"), headers=h, follow_redirects=False)
    assert r.headers["location"] == f"{pre}/login?next=1"


def test_javascript_root_redirect_stays_inside_console_prefix(app_client, owned, upstream):
    li, h = owned
    body = b'window.location = "/webfig/";'
    upstream.respond = lambda req: httpx.Response(
        200,
        content=body,
        headers={
            "content-type": "application/javascript; charset=utf-8",
            "etag": '"device-script-v1"',
        },
    )

    r = app_client.get(_url(li, "assets/script.js"), headers=h)

    # Executable source outside the pinned firmware closure is refused rather
    # than being streamed before it can be authenticated.
    assert r.status_code == 502
    assert "etag" not in r.headers

    upstream.respond = lambda req: httpx.Response(
        200,
        content=b"binary:/webfig/:payload",
        headers={"content-type": "application/octet-stream"},
    )
    binary = app_client.get(_url(li, "jsproxy"), headers=h)
    assert binary.content == b"binary:/webfig/:payload"


def test_upstream_down_is_502(app_client, owned, upstream):
    li, h = owned

    def boom(req):
        raise httpx.ConnectError("down")

    upstream.respond = boom
    assert app_client.get(_url(li, "x"), headers=h).status_code == 502


# -- CSRF: the bridge is narrow and fail-closed --------------------------------


def test_post_without_origin_still_csrf_rejected(app_client, owned, upstream):
    li, h = owned
    r = app_client.post(_url(li), headers=h, content=b"x")
    assert r.status_code == 403 and not upstream.calls


@pytest.mark.parametrize(
    "extra",
    [
        {"Origin": "http://evil.test"},
        {**ORIGIN, "Sec-Fetch-Site": "cross-site"},
        {"Origin": "null"},
        {"Origin": "https://alpha.localhost"},
        {"Origin": "http://alpha.localhost/path"},
    ],
)
def test_cross_site_post_csrf_rejected(app_client, owned, upstream, extra):
    li, h = owned
    r = app_client.post(_url(li), headers={**h, **extra}, content=b"x")
    assert r.status_code == 403 and not upstream.calls


def test_bridge_does_not_apply_to_other_routes(app_client, owned, upstream):
    li, h = owned
    r = app_client.post(f"/labs/instances/{li.id}/check", headers={**h, **ORIGIN})
    assert r.status_code == 403
    r = app_client.post(f"/labs/instances/{li.id}/console/r1", headers={**h, **ORIGIN})
    assert r.status_code in (403, 405)  # index route is GET-only; never bridged


def test_webfig_notification_long_poll_has_no_read_timeout() -> None:
    """RouterOS keeps one authenticated ``jsproxy`` request open indefinitely."""
    timeout = console_upstream_timeout()

    assert timeout.read is None
    assert timeout.connect == 30.0
    assert timeout.write == 30.0
    assert timeout.pool == 30.0


def test_webfig_root_redirect_is_rewritten_inside_console_prefix() -> None:
    prefix = "/labs/instances/12345678-1234-1234-1234-123456789abc/console/r1"
    body = b'window.location = "/webfig/";'
    rewriter = WebFigRootRewriter(prefix)

    assert rewriter.feed(body) + rewriter.finish() == (b'window.location = "' + prefix.encode() + b'/webfig/";')


def test_webfig_root_rewrite_survives_every_chunk_boundary() -> None:
    prefix = "/labs/instances/12345678-1234-1234-1234-123456789abc/console/r1"
    body = b"before:/webfig/:after"
    expected = b"before:" + prefix.encode() + b"/webfig/:after"

    for split in range(len(body) + 1):
        rewriter = WebFigRootRewriter(prefix)
        actual = rewriter.feed(body[:split]) + rewriter.feed(body[split:]) + rewriter.finish()
        assert actual == expected


def test_only_text_and_javascript_responses_are_rewritten() -> None:
    assert should_rewrite_webfig_body("text/html; charset=utf-8")
    assert should_rewrite_webfig_body("text/javascript")
    assert should_rewrite_webfig_body("application/javascript")
    assert not should_rewrite_webfig_body("application/octet-stream")
    assert not should_rewrite_webfig_body(None)


@pytest.fixture(params=[False, True], ids=["development-cookie", "production-host-cookie"])
def signed_console_client(request, monkeypatch, upstream):
    """Exercise the installed a107 signer and bridge without any database."""
    from app.kernel_runtime import ConsoleProxyCsrfBridge

    production = request.param
    monkeypatch.setattr(settings, "environment", "production" if production else "development")
    app = FastAPI()

    @app.api_route(
        "/labs/instances/{instance_id}/console/{node}/{subpath:path}",
        methods=["GET", "HEAD", "POST"],
        dependencies=[Depends(require_csrf)],
    )
    async def proxy(request: Request):
        return await labs._proxy_http(request, "http://10.123.0.10/jsproxy", webfig=True)

    @app.api_route("/other", methods=["GET", "POST"], dependencies=[Depends(require_csrf)])
    async def other():
        return Response(content=b"ok")

    app.add_middleware(
        CSRFMiddleware,
        secret="console-test-csrf-secret",
        production=production,
        session_cookie_names=("session",),
    )
    app.add_middleware(ConsoleProxyCsrfBridge)
    with TestClient(app, base_url="https://alpha.localhost") as client:
        client.cookies.set("session", "test-session", domain="alpha.localhost", path="/")
        client.get("/other")
        yield client


_SIGNED_URL = "/labs/instances/12345678-1234-1234-1234-123456789abc/console/r1/jsproxy"
_SIGNED_ORIGIN = {"Origin": "https://alpha.localhost"}


def test_signed_bridge_preserves_session_bound_validation(signed_console_client, upstream):
    client = signed_console_client
    assert client.post(_SIGNED_URL, headers=_SIGNED_ORIGIN, content=b"payload").status_code == 200
    assert upstream.calls[-1].content == b"payload"
    assert "session" not in upstream.calls[-1].headers.get("cookie", "")
    upstream.calls.clear()
    client.cookies.set("session", "changed-session", domain="alpha.localhost", path="/")
    assert client.post(_SIGNED_URL, headers=_SIGNED_ORIGIN, content=b"payload").status_code == 403
    assert not upstream.calls


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": "https://evil.test"},
        {"Origin": "null"},
        {"Origin": "http://alpha.localhost"},
        {"Origin": "https://alpha.localhost/escape"},
        {**_SIGNED_ORIGIN, "Sec-Fetch-Site": "cross-site"},
        {**_SIGNED_ORIGIN, "x-csrf-token": "invalid"},
        {"Origin": "https://evil.test", "X-Forwarded-Host": "evil.test"},
        {"Origin": "http://alpha.localhost", "X-Forwarded-Proto": "https"},
        {"Origin": "http://alpha.localhost", "X-Forwarded-Proto": "http"},
    ],
)
def test_signed_bridge_rejects_missing_foreign_and_invalid_proof(signed_console_client, upstream, headers):
    assert signed_console_client.post(_SIGNED_URL, headers=headers, content=b"x").status_code == 403
    assert not upstream.calls


def test_signed_bridge_keeps_other_routes_protected(signed_console_client, upstream):
    assert signed_console_client.post("/other", headers=_SIGNED_ORIGIN).status_code == 403
    assert not upstream.calls


def test_signed_bridge_forwarding_scheme_conflict_remains_fail_closed(signed_console_client, upstream):
    response = signed_console_client.post(
        _SIGNED_URL, headers={**_SIGNED_ORIGIN, "X-Forwarded-Proto": "http"}, content=b"x"
    )
    # Kernel a107 also checks origin provenance; contradictory raw forwarding
    # evidence must remain refused without changing the installed validator.
    assert response.status_code == 403
    assert not upstream.calls


@pytest.mark.parametrize("segment", ["%2e", "%2E", "%2e%2e", ".%2e", "%2E.", "%2E%2e"])
def test_device_redirect_rejects_browser_encoded_dot_traversal(segment):
    from app.web.console_proxy import rewrite_location

    prefix = "/labs/instances/12345678-1234-1234-1234-123456789abc/console/r1"
    location = "/" + (segment + "/") * 8 + "login"
    assert rewrite_location(location, "http://10.123.0.10/webfig/", prefix) is None


@pytest.mark.parametrize(
    "location", ["/a\\b", "/a\nb", "/a\rb", "/a\x00b", "/a\x7fb", "http://[", "//[evil", "http://[bad]/"]
)
def test_device_redirect_rejects_browser_path_controls(location):
    from app.web.console_proxy import rewrite_location

    assert rewrite_location(location, "http://10.123.0.10/webfig/", "/console/r1") is None


def test_device_redirect_preserves_encoded_resource_separators():
    from app.web.console_proxy import rewrite_location

    assert rewrite_location("/a%2Fb%3Fc?q=x", "http://10.123.0.10/webfig/", "/console/r1") == (
        "/console/r1/a%2Fb%3Fc?q=x"
    )


def test_signed_bridge_caps_and_sanitizes_proxy(signed_console_client, upstream, monkeypatch):
    monkeypatch.setattr(settings, "lab_console_max_body_bytes", 4)
    client = signed_console_client
    assert client.post(_SIGNED_URL, headers=_SIGNED_ORIGIN, content=b"12345").status_code == 413
    assert not upstream.calls
    headers = {
        **_SIGNED_ORIGIN,
        "Authorization": "Bearer test-only",
        "X-Forwarded-For": "192.0.2.1",
        "Referer": "https://alpha.localhost/private",
        "Content-Type": "application/octet-stream",
    }
    assert client.post(_SIGNED_URL, headers=headers, content=b"1234").status_code == 200
    call = upstream.calls[-1]
    assert call.content == b"1234"
    assert call.headers["content-type"] == "application/octet-stream"
    for name in ("authorization", "x-forwarded-for", "referer", "origin", "cookie", "x-csrf-token"):
        assert name not in call.headers


def test_signed_dynamic_reply_is_not_rewritten(signed_console_client, upstream):
    upstream.respond = lambda req: httpx.Response(
        200,
        content=b'window.location="/webfig/";',
        headers={"content-type": "application/javascript", "etag": '"original"'},
    )
    result = signed_console_client.get(_SIGNED_URL)
    assert result.content == b'window.location="/webfig/";'
    assert result.headers["etag"] == '"original"'
    assert result.headers["content-type"] == "application/octet-stream"
    assert result.headers["x-content-type-options"] == "nosniff"
    assert result.headers["content-disposition"] == "attachment"


@pytest.mark.parametrize(
    "value",
    [
        "localhost",
        "127.0.0.1",
        "169.254.169.254",
        "8.8.8.8",
        "10.1.2.3:8080",
        "u@10.1.2.3",
        "::1",
        "10.1.2.3/x",
        None,
    ],
)
def test_management_target_validation_without_network(value):
    assert safe_mgmt_address(value) is None
    assert safe_mgmt_address("10.123.0.10") == "10.123.0.10"


def test_signed_proxy_caps_chunked_body(signed_console_client, upstream, monkeypatch):
    monkeypatch.setattr(settings, "lab_console_max_body_bytes", 4)
    response = signed_console_client.post(
        _SIGNED_URL,
        headers=_SIGNED_ORIGIN,
        content=(b"123" for _ in range(2)),
    )
    assert response.status_code == 413
    assert not upstream.calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "subpath", "opening"),
    [
        ("GET", "", True),
        ("GET", "jsproxy", False),
        ("POST", "jsproxy", False),
        ("POST", "", False),
        ("HEAD", "", False),
        ("GET", "webfig/", False),
    ],
)
async def test_only_trailing_slash_get_records_opening(monkeypatch, method, subpath, opening):
    from types import SimpleNamespace
    from uuid import uuid4

    instance_id = uuid4()
    tenant = SimpleNamespace(id=uuid4())
    events = []
    request = Request({"type": "http", "method": method, "headers": []})
    monkeypatch.setattr(labs, "require_tenant", lambda req: tenant)

    def authorize(*args):
        events.append("authorized")
        return "http://10.123.0.10/"

    def note(*args):
        events.append("opening")

    async def proxy(req, target, *, webfig=False, reviewed_document=False):
        events.append("proxy")
        return Response(content=b"ok")

    monkeypatch.setattr(labs, "_authorize_console", authorize)
    monkeypatch.setattr(labs, "_note_console_use", note)
    monkeypatch.setattr(labs, "_proxy_http", proxy)
    await labs.console_subpath(instance_id, "r1", subpath, request, person=object(), db=object())
    assert events == (["authorized", "opening", "proxy"] if opening else ["authorized", "proxy"])


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_bare_webfig_redirect_is_authorized_and_keeps_query(app_client, owned, upstream, method):
    li, h = owned
    response = app_client.request(method, _url(li, "webfig") + "?a=1&b=%2F", headers=h, follow_redirects=False)
    assert response.status_code == 308
    assert response.headers["location"] == _url(li, "webfig/") + "?a=1&b=%2F"
    assert not upstream.calls


def test_bare_webfig_redirect_does_not_bypass_owner_gate(app_client, owned, upstream, admin_session, tenant_a):
    li, _ = owned
    _make_person(admin_session, tenant_a, "not-webfig-owner@a.edu")
    h = _login(app_client, "not-webfig-owner@a.edu")
    response = app_client.get(_url(li, "webfig"), headers=h, follow_redirects=False)
    assert response.status_code == 403
    assert "location" not in response.headers
    assert not upstream.calls
