"""Pinned RouterOS 7.15 assets adapted for Academy's existing strict CSP.

This is a reviewed source adapter, never a JavaScript evaluator. Every asset is
verified in full before transformation and before sending response headers.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import zlib
from pathlib import Path

import httpx
from fastapi import HTTPException

TRANSFORMATION_VERSION = 1
MAX_ASSET_BYTES = 2 * 1024 * 1024
MAX_WIRE_BYTES = 2 * 1024 * 1024
WIRE_CHUNK_BYTES = 64 * 1024
MANIFEST_PATH = Path(__file__).with_name("webfig_assets_v1.json")
_SCRIPT_TYPES = {
    "application/javascript",
    "application/x-javascript",
    "text/javascript",
    "application/ecmascript",
    "application/x-ecmascript",
    "text/ecmascript",
    "text/javascript1.0",
    "text/javascript1.1",
    "text/javascript1.2",
    "text/javascript1.3",
    "text/javascript1.4",
    "text/javascript1.5",
    "text/jscript",
    "text/livescript",
    "text/x-javascript",
    "text/x-ecmascript",
}
_PASSIVE_TYPES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/avif",
        "image/x-icon",
        "image/vnd.microsoft.icon",
        "font/woff",
        "font/woff2",
        "font/ttf",
        "font/otf",
        "application/font-woff",
        "application/x-font-ttf",
        "application/x-font-opentype",
        "application/vnd.ms-fontobject",
    }
)
_KINDS = {"document", "master", "script", "data", "static"}
_PARSERS = (
    b'<script src="/static/console/acorn-8.19.0.js"></script><script src="/static/console/webfig-data-v1.js"></script>'
)
_EVAL_PATCHES = (
    (b"eval(`([${list}{}])`)", b"globalThis.AcademyWebFigDataV1.parseList(list)"),
    (b"eval(`(${map})`)", b"globalThis.AcademyWebFigDataV1.parseSchema(map)"),
    (b"eval('('+resp+')')", b"globalThis.AcademyWebFigDataV1.parseSkin(resp)"),
    (b"skin=JSON.parse(resp)", b"skin=globalThis.AcademyWebFigDataV1.parseSkin(resp)"),
)
# createMenuItem returns the <li>; its first child is the anchor. Bind a
# closure over this iteration's name, cancel hash navigation, and retain the
# existing openGroup DOM toggling without an inline JavaScript URL.
_GROUP_MENU_PATCH = (
    b'let item=createMenuItem(menu,name,cfg,`javascript:openGroup("${name}")`,menuItem.icon);if(!item)continue;',
    b'let item=createMenuItem(menu,name,cfg,"#",menuItem.icon);if(!item)continue;'
    b"item.firstChild.onclick=(group=>e=>{e.preventDefault();openGroup(group);})(name);",
)
# Complete URL literals only: '/' itself is deliberately excluded because it is
# also used as data and a delimiter by RouterOS. The fixed logout context below
# is the one reviewed root-navigation site.


def refused() -> HTTPException:
    return HTTPException(status_code=502, detail="unsupported lab console asset")


def load_manifest() -> dict:
    try:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        if manifest["transformation_version"] != TRANSFORMATION_VERSION:
            raise ValueError("adapter version")
        if manifest["firmware"]["version"] != "7.15":
            raise ValueError("firmware version")
        if not re.fullmatch(r"[^\s]+@sha256:[a-f0-9]{64}", manifest["firmware"]["image_repo_digest"]):
            raise ValueError("image identity")
        assets = manifest["assets"]
        if not isinstance(assets, dict) or not assets:
            raise ValueError("assets")
        for path, asset in assets.items():
            if not path.startswith("/") or "?" in path or "#" in path or ".." in path.split("/"):
                raise ValueError("asset path")
            if asset["kind"] not in _KINDS or type(asset["bytes"]) is not int:
                raise ValueError("asset kind/size")
            if not 0 < asset["bytes"] <= MAX_ASSET_BYTES or not re.fullmatch(r"[a-f0-9]{64}", asset["sha256"]):
                raise ValueError("asset identity")
        return manifest
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise refused() from exc


def is_dynamic_path(path: str) -> bool:
    """Encrypted session traffic must never be interpreted as source code."""
    return path == "/jsproxy" or path.startswith("/jsproxy/")


def active_content(content_type: str | None, path: str) -> bool:
    media = (content_type or "").partition(";")[0].strip().lower()
    return (
        media in _SCRIPT_TYPES
        or media in {"text/html", "application/xhtml+xml", "text/css", "image/svg+xml"}
        or path.lower().endswith((".js", ".mjs", ".cjs", ".jg", ".css", ".html", ".htm", ".svg"))
        or path in {"/", "/webfig", "/webfig/", "/webfig/list"}
    )


def passive_content(content_type: str | None) -> bool:
    """Unknown paths may serve only browser-enforced passive media types.

    In particular no text/plain or application/octet-stream sniffing is trusted;
    executable and unrecognized MIME values require a pinned manifest entry.
    """
    media = (content_type or "").partition(";")[0].strip().lower()
    return media in _PASSIVE_TYPES


def rewrite_url_literals(body: bytes, prefix: str) -> bytes:
    """Rewrite exact reviewed URL literal forms, retaining query/fragment bytes."""
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", prefix) or prefix.endswith("/") or "//" in prefix:
        raise refused()
    if any(segment in {".", ".."} for segment in prefix.split("/")):
        raise refused()
    replacement = prefix.encode("ascii")
    # Exact literals from the pinned source, never a JavaScript quote regex.
    literals = (
        b"/webfig",
        b"/webfig/",
        b"/webfig/list",
        b"/webfig/${gum.unique}",
        b"/webfig/${window.location.hash}",
        b"/jsproxy",
        b"/jsproxy/",
        b"/jsproxy/?",
        b"/jsproxy/upload?",
        b"/jsproxy/put?",
        b"/files",
        b"/graphs",
        b"/graphs/",
        b"/help/license.html",
        b"/favicon.png",
        b"/favicon.svg",
        b"/logo.png",
        b"/logo.svg",
    )
    # These exact attribute contexts also cover the manifest-pinned hashed
    # script/style filenames, without treating arbitrary JavaScript fields as
    # URL literals. The master's "/files/" field is a device filesystem path.
    for quote in (b'"', b"'"):
        for attribute in (b"href=", b"src=", b"action="):
            for root in (b"/webfig/", b"/files/"):
                old = attribute + quote + root
                body = body.replace(old, attribute + quote + replacement + root)
    for literal in literals:
        for quote in (b'"', b"'", b"`"):
            body = body.replace(quote + literal + quote, quote + replacement + literal + quote)
    return body.replace(
        b"window.location.replace(`/${window.location.hash}`)",
        b"window.location.replace(`" + replacement + b"/${window.location.hash}`)",
    )


def transform_asset(body: bytes, asset: dict, prefix: str) -> bytes:
    if len(body) != asset["bytes"] or hashlib.sha256(body).hexdigest() != asset["sha256"]:
        raise refused()
    kind = asset["kind"]
    if kind == "master":
        for old, new in (*_EVAL_PATCHES, _GROUP_MENU_PATCH):
            if body.count(old) != 1:
                raise refused()
            body = body.replace(old, new)
    if kind == "document":
        # Only this exact reviewed redundant redirect may be removed. Other
        # inline source is refused; it cannot be enabled under the strict CSP.
        redirect = asset.get("redundant_inline_redirect")
        if redirect is not None:
            encoded = redirect.encode("utf-8")
            if body.count(encoded) != 1 or not encoded.startswith(b"<script") or not encoded.endswith(b"</script>"):
                raise refused()
            body = body.replace(encoded, b"")
        master_script = asset.get("master_script_tag")
        if master_script is not None:
            tag = master_script.encode("utf-8")
            if body.count(tag) != 1 or not tag.startswith(b"<script ") or b"src=" not in tag:
                raise refused()
            body = body.replace(tag, _PARSERS + tag)
        if re.search(rb"<script\b(?![^>]*\bsrc\s*=)[^>]*>\s*\S", body, re.I):
            raise refused()
    if kind in {"master", "script", "document"} or (
        kind == "static" and asset.get("content_type") in {"text/css", "image/svg+xml"}
    ):
        body = rewrite_url_literals(body, prefix)
    return body


async def read_asset(upstream: httpx.Response, asset: dict) -> bytes:
    """Bound wire and decoded bytes separately before hashing an entity.

    RouterOS 7.15 may refuse identity requests. Gzip here is a transport detail,
    not a relaxation of the pinned decoded-source identity.
    """
    if upstream.status_code != 200 or upstream.headers.get("content-range") is not None:
        raise refused()
    encoding = upstream.headers.get("content-encoding", "identity").strip().lower()
    if encoding not in {"identity", "gzip"}:
        raise refused()
    declared = upstream.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > MAX_WIRE_BYTES):
        raise refused()
    expected = asset["bytes"]
    if type(expected) is not int or not 0 < expected <= MAX_ASSET_BYTES:
        raise refused()
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding == "gzip" else None
    body = bytearray()
    wire_size = 0
    try:
        async with asyncio.timeout(15):
            async for chunk in upstream.aiter_raw(chunk_size=WIRE_CHUNK_BYTES):
                wire_size += len(chunk)
                if len(chunk) > WIRE_CHUNK_BYTES or wire_size > MAX_WIRE_BYTES:
                    raise refused()
                remaining = expected - len(body)
                decoded = decoder.decompress(chunk, remaining + 1) if decoder is not None else chunk
                if len(decoded) > remaining:
                    raise refused()
                body.extend(decoded)
                if decoder is not None and (decoder.unconsumed_tail or decoder.unused_data):
                    # Reject concatenated gzip members and trailing data too.
                    raise refused()
            if decoder is not None:
                if not decoder.eof or decoder.unconsumed_tail or decoder.unused_data:
                    raise refused()
                # EOF with all input consumed means decompress has delivered
                # all output. Flush only this completed state, never a truncated
                # stream or a decoder stopped at its max_length boundary.
                final = decoder.flush(1)
                if len(final) > expected - len(body):
                    raise refused()
                body.extend(final)
    except (httpx.HTTPError, TimeoutError, zlib.error) as exc:
        raise refused() from exc
    if declared is not None and wire_size != int(declared):
        raise refused()
    if len(body) != expected or hashlib.sha256(body).hexdigest() != asset["sha256"]:
        raise refused()
    return bytes(body)
