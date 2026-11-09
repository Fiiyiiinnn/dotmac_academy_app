"""Fail-closed source adaptation; runtime execution belongs to hosted CI."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from app.web import labs
from app.web import webfig_adapter as adapter
from app.web.console_proxy import rewrite_location

pytestmark = pytest.mark.private_fixture_data

PREFIX = "/labs/instances/12345678-1234-1234-1234-123456789abc/console/r1"


def asset(body, kind="script", **extra):
    return {"bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(), "kind": kind, **extra}


def test_paths_preserve_data_regex_query_fragment_and_prefix():
    body = b"""const field="/";const rx=/\\//;fetch("/webfig");fetch('/jsproxy/upload?');
const already="/labs/instances/12345678-1234-1234-1234-123456789abc/console/r1/webfig";
window.location.replace(`/${window.location.hash}`);const other="/jsproxy-unrelated";const storage=base+"/files/"+name;"""
    out = adapter.rewrite_url_literals(body, PREFIX)
    assert b'const field="/";const rx=/\\//;' in out
    assert b'fetch("' + PREFIX.encode() + b'/webfig");' in out
    assert b"fetch('" + PREFIX.encode() + b"/jsproxy/upload?');" in out
    assert out.count(PREFIX.encode()) == 4
    assert b'const other="/jsproxy-unrelated"' in out
    assert b'const storage=base+"/files/"+name' in out
    assert b"/${window.location.hash}`)" in out
    assert adapter.rewrite_url_literals(out, PREFIX) == out


def test_master_all_evaluation_sites_replaced_and_no_json_bypass():
    body = b";".join(old for old, _ in (*adapter._EVAL_PATCHES, adapter._GROUP_MENU_PATCH))
    out = adapter.transform_asset(body, asset(body, "master"), PREFIX)
    assert b"javascript:" not in out
    assert b"openGroup(group)" in out
    assert b"})(name)" in out
    assert b"e.preventDefault()" in out
    assert b"eval(" not in out
    assert b"JSON.parse(resp)" not in out
    assert out.count(b"parseSkin(resp)") == 2
    for broken in (body + b" ", body.replace(adapter._EVAL_PATCHES[0][0], b"different")):
        with pytest.raises(HTTPException) as error:
            adapter.transform_asset(broken, asset(body, "master"), PREFIX)
        assert error.value.status_code == 502


def test_document_external_parser_order_and_inline_refusal():
    tag = '<script src="/webfig/master.js"></script>'
    redirect = '<script>window.location="/webfig/";</script>'
    body = ("<html>" + redirect + tag + "</html>").encode()
    out = adapter.transform_asset(
        body, asset(body, "document", redundant_inline_redirect=redirect, master_script_tag=tag), PREFIX
    )
    assert b"window.location" not in out
    assert PREFIX.encode() + b'/webfig/master.js"' in out
    assert out.index(b"acorn-8.19.0.js") < out.index(b"webfig-data-v1.js") < out.index(b"master.js")
    unexpected = b"<script>alert(1)</script>"
    with pytest.raises(HTTPException) as error:
        adapter.transform_asset(unexpected, asset(unexpected, "document"), PREFIX)
    assert error.value.status_code == 502


@pytest.mark.parametrize(
    "headers,status",
    [
        ({"content-encoding": "br"}, 200),
        ({}, 206),
        ({"content-range": "bytes 0-2/3"}, 200),
        ({"content-length": "999"}, 200),
    ],
)
def test_partial_compressed_and_false_length_refused(headers, status):
    upstream = httpx.Response(status, headers=headers, stream=httpx.ByteStream(b"abc"))
    with pytest.raises(HTTPException) as error:
        asyncio.run(adapter.read_asset(upstream, asset(b"abc")))
    assert error.value.status_code == 502


def test_oversized_stream_refused():
    upstream = httpx.Response(200, stream=httpx.ByteStream(b"abcd"))
    with pytest.raises(HTTPException) as error:
        asyncio.run(adapter.read_asset(upstream, asset(b"abc")))
    assert error.value.status_code == 502


def test_unknown_firmware_manifest_refused(monkeypatch, tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"transformation_version": 1, "firmware": {"version": "7.16"}}))
    monkeypatch.setattr(adapter, "MANIFEST_PATH", path)
    with pytest.raises(HTTPException) as error:
        adapter.load_manifest()
    assert error.value.status_code == 502


@pytest.mark.parametrize("drift", [False, True])
@pytest.mark.parametrize("encoding", ["identity", "gzip"])
def test_proxy_checks_before_headers_and_closes_and_preserves_csp(monkeypatch, drift, encoding):
    original = b'fetch("/webfig")'
    closed = []
    source = original + (b" " if drift else b"")
    wire = gzip.compress(source, mtime=0) if encoding == "gzip" else source

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for offset in range(0, len(wire), 7):
                yield wire[offset : offset + 7]

        async def aclose(self):
            closed.append("response")

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            assert request.headers["accept-encoding"] == "gzip"
            assert "range" not in request.headers
            return httpx.Response(
                200,
                headers=[
                    ("content-type", "application/javascript"),
                    ("content-encoding", encoding),
                    ("content-length", str(len(wire))),
                    ("etag", "provider-source"),
                    ("content-security-policy", "script-src 'self'"),
                    ("content-security-policy", "object-src 'none'"),
                    ("x-content-type-options", "nosniff"),
                ],
                stream=Stream(),
            )

        async def aclose(self):
            closed.append("client")

    monkeypatch.setattr(adapter, "load_manifest", lambda: {"assets": {"/webfig/master.js": asset(original)}})
    monkeypatch.setattr(labs, "_new_upstream_client", lambda: httpx.AsyncClient(transport=Transport()))
    app = FastAPI()

    @app.get("/labs/instances/{instance_id}/console/{node}/webfig/master.js")
    async def proxy(request: Request):
        return await labs._proxy_http(request, "http://10.0.0.2/webfig/master.js", webfig=True)

    with TestClient(app) as client:
        response = client.get(PREFIX + "/webfig/master.js", headers={"range": "bytes=0-1"})
    assert closed == ["response", "client"]
    assert response.status_code == (502 if drift else 200)
    if not drift:
        assert response.headers.get_list("content-security-policy") == ["script-src 'self'", "object-src 'none'"]
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["cache-control"] == "no-store"
        assert b"unsafe-" not in response.content
        assert "content-encoding" not in response.headers
        assert "etag" not in response.headers
        assert response.content == b'fetch("' + PREFIX.encode() + b'/webfig")'


@pytest.mark.parametrize("path", ["/new.js", "/webfig/new.jg", "/webfig/list", "/"])
def test_unknown_active_assets_fail_closed(path):
    assert adapter.active_content(None, path)
    assert not adapter.is_dynamic_path(path)


def test_encrypted_payloads_are_never_source_assets():
    for path in ("/jsproxy", "/jsproxy/", "/jsproxy/upload", "/jsproxy/put"):
        assert adapter.is_dynamic_path(path)
    assert not adapter.is_dynamic_path("/jsproxy-other")


def test_redirect_keeps_query_and_fragment_inside_proxy():
    assert (
        rewrite_location("/webfig/?a=1#Interfaces", "http://10.0.0.2/", PREFIX, preserve_fragment=True)
        == PREFIX + "/webfig/?a=1#Interfaces"
    )


def test_data_schema_payload_is_not_rewritten():
    body = b'{name:"/webfig/",path:"/files/"}'
    assert adapter.transform_asset(body, asset(body, "data"), PREFIX) == body


class SplitWire(httpx.AsyncByteStream):
    """Genuine unread stream: transport bytes arrive across gzip boundaries."""

    def __init__(self, wire):
        self.wire = wire

    async def __aiter__(self):
        for offset in range(0, len(self.wire), 7):
            yield self.wire[offset : offset + 7]


def gzip_response(wire, *, declared=None):
    headers = {"content-encoding": "gzip", "content-length": str(len(wire) if declared is None else declared)}
    return httpx.Response(200, headers=headers, stream=SplitWire(wire))


def test_gzip_stream_uses_wire_length_and_decoded_identity():
    body = b"RouterOS source;" * 10000
    wire = gzip.compress(body, mtime=0)
    assert len(wire) != len(body)
    assert asyncio.run(adapter.read_asset(gzip_response(wire), asset(body))) == body


@pytest.mark.parametrize(
    "corruption", ["truncated", "extra_member", "trailing", "bomb", "wrong_length", "malformed", "wrong_identity"]
)
def test_gzip_corruption_fails_before_success(corruption):
    body = b"known source" * 100
    wire = gzip.compress(body, mtime=0)
    declared = None
    if corruption == "truncated":
        wire = wire[:-3]
    elif corruption == "extra_member":
        wire += gzip.compress(b"", mtime=0)
    elif corruption == "trailing":
        wire += b"unreviewed trailer"
    elif corruption == "bomb":
        wire = gzip.compress(b"A" * (adapter.MAX_ASSET_BYTES + 1), mtime=0)
    elif corruption == "wrong_length":
        declared = len(wire) + 1
    elif corruption == "malformed":
        wire = b"not gzip data"
    elif corruption == "wrong_identity":
        wire = gzip.compress(b"x" * len(body), mtime=0)
    with pytest.raises(HTTPException) as error:
        asyncio.run(adapter.read_asset(gzip_response(wire, declared=declared), asset(body)))
    assert error.value.status_code == 502


def test_oversized_gzip_wire_is_refused(monkeypatch):
    body = b"a valid entity"
    wire = gzip.compress(body, mtime=0)
    monkeypatch.setattr(adapter, "MAX_WIRE_BYTES", len(wire) - 1)
    upstream = httpx.Response(200, headers={"content-encoding": "gzip"}, stream=SplitWire(wire))
    with pytest.raises(HTTPException) as error:
        asyncio.run(adapter.read_asset(upstream, asset(body)))
    assert error.value.status_code == 502


FIXTURE_ROOT = Path(__file__).with_name("fixtures") / "webfig"
FIXTURE_MANIFEST = json.loads((FIXTURE_ROOT / "manifest.json").read_text())


@pytest.mark.parametrize("path", sorted(FIXTURE_MANIFEST["assets"]))
def test_real_firmware_closure_is_bounded_hashed_and_adapted(path):
    production = adapter.load_manifest()
    fixture = FIXTURE_MANIFEST["assets"][path]
    spec = production["assets"][path]
    assert {key: spec[key] for key in ("sha256", "bytes", "kind")} == {
        key: fixture[key] for key in ("sha256", "bytes", "kind")
    }
    wire = (FIXTURE_ROOT / fixture["path"]).read_bytes()
    source = asyncio.run(adapter.read_asset(gzip_response(wire), spec))
    output = adapter.transform_asset(source, spec, PREFIX)
    if spec["kind"] == "master":
        assert b"eval(" not in output
        assert b"javascript:" not in output
        assert b"JSON.parse(resp)" not in output
        assert output.count(b"AcademyWebFigDataV1.parseSkin(resp)") == 2
        assert b"AcademyWebFigDataV1.parseSchema(map)" in output
        assert b"AcademyWebFigDataV1.parseList(list)" in output
        assert b"openGroup(group);})(name)" in output
        assert b'+"/files/"+' in output  # device filesystem data remains unchanged
        for root in (b'"/webfig"', b"'/jsproxy'", b'"/jsproxy/upload?"', b'"/logo.png"'):
            assert root not in output
    elif path == "/webfig/":
        assert production["assets"][path]["redundant_inline_redirect"].encode() not in output
        assert output.index(b"acorn-8.19.0.js") < output.index(b"webfig-data-v1.js") < output.index(b"master-min-")
        assert PREFIX.encode() + b'/favicon.png"' in output
    elif path == "/script.js":
        assert b"window.location.replace(`" + PREFIX.encode() + b"/webfig/${window.location.hash}`)" in output
    elif spec["kind"] == "data" or spec.get("content_type", "").startswith("image/png"):
        assert output == source
    if spec["kind"] in {"document", "script", "master", "data"}:
        changed = bytes([source[0] ^ 1]) + source[1:]
        with pytest.raises(HTTPException) as error:
            adapter.transform_asset(changed, spec, PREFIX)
        assert error.value.status_code == 502


def test_dotted_owned_node_prefix_is_safe_without_allowing_dot_segments():
    prefix = PREFIX.rsplit("/", 1)[0] + "/r1.edge"
    assert adapter.rewrite_url_literals(b'fetch("/webfig")', prefix) == b'fetch("' + prefix.encode() + b'/webfig")'
    for unsafe in (PREFIX + "/../r2", PREFIX + "/./r2", PREFIX + "//r2", PREFIX + '";alert(1)'):
        with pytest.raises(HTTPException):
            adapter.rewrite_url_literals(b'fetch("/webfig")', unsafe)


@pytest.mark.parametrize(
    ("path", "media", "expected"),
    [
        ("/files/extensionless", "application/ecmascript", 502),
        ("/extensionless", "text/ecmascript", 502),
        ("/extensionless", "text/javascript1.5", 502),
        ("/extensionless", "text/x-javascript", 502),
        ("/files/extensionless", "application/octet-stream", 502),
        ("/files/extensionless", "text/plain", 502),
        ("/files/extensionless", None, 502),
        ("/files/payload.JS", "image/png", 502),
        ("/files/payload.mjs", "image/png", 502),
        ("/files/payload.MJS", "application/ecmascript", 502),
        ("/unreviewed.svg", "image/svg+xml", 502),
        ("/files/picture", "image/png", 200),
        ("/files/font", "font/woff2", 200),
        ("/jsproxy/reply.mjs", "application/ecmascript", 200),
        ("/jsproxy", "application/javascript", 200),
        ("/jsproxy", "application/octet-stream", 200),
    ],
)
def test_unknown_proxy_resources_require_passive_media_except_jsproxy(monkeypatch, path, media, expected):
    closed = []
    reads = []
    requests = []
    body = b"unreviewed payload bytes"

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            reads.append("body")
            yield body

        async def aclose(self):
            closed.append("response")

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            requests.append(request)
            headers = [] if media is None else [("content-type", media)]
            headers.extend(
                [
                    ("content-security-policy", "script-src 'self'"),
                    ("content-security-policy", "object-src 'none'"),
                    ("set-cookie", "sid=device; Path=/"),
                ]
            )
            return httpx.Response(200, headers=headers, stream=Stream())

        async def aclose(self):
            closed.append("client")

    monkeypatch.setattr(adapter, "load_manifest", lambda: {"assets": {}})
    monkeypatch.setattr(labs, "_new_upstream_client", lambda: httpx.AsyncClient(transport=Transport()))
    app = FastAPI()

    @app.get("/labs/instances/{instance_id}/console/{node}/{subpath:path}")
    async def proxy(request: Request):
        return await labs._proxy_http(request, "http://10.0.0.2" + path, webfig=True)

    with TestClient(app) as client:
        response = client.get(PREFIX + path)
    assert response.status_code == expected
    if not requests:
        assert not reads and not closed  # rejected before allocating a client
    else:
        assert closed == ["response", "client"]
        assert reads == (["body"] if expected == 200 else [])
    if expected == 200:
        assert response.content == body
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers.get_list("content-security-policy") == ["script-src 'self'", "object-src 'none'"]
        assert response.headers["set-cookie"].startswith("wf_sid=device; Path=" + PREFIX + "/;")
        if adapter.is_dynamic_path(path):
            assert response.headers["content-type"] == "application/octet-stream"
            assert response.headers["content-disposition"] == "attachment"


def test_unknown_redirect_preserves_confined_location_but_discards_unpinned_body(monkeypatch):
    closed = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            pytest.fail("unreviewed redirect source must not be consumed or served")
            yield b""  # pragma: no cover

        async def aclose(self):
            closed.append("response")

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return httpx.Response(
                302,
                headers={"location": "/webfig/#Interfaces", "content-type": "application/ecmascript"},
                stream=Stream(),
            )

        async def aclose(self):
            closed.append("client")

    monkeypatch.setattr(adapter, "load_manifest", lambda: {"assets": {}})
    monkeypatch.setattr(labs, "_new_upstream_client", lambda: httpx.AsyncClient(transport=Transport()))
    app = FastAPI()

    @app.get("/labs/instances/{instance_id}/console/{node}/redirect")
    async def proxy(request: Request):
        return await labs._proxy_http(request, "http://10.0.0.2/redirect", webfig=True)

    with TestClient(app) as client:
        response = client.get(PREFIX + "/redirect", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == PREFIX + "/webfig/#Interfaces"
    assert response.content == b""
    assert closed == ["response", "client"]
