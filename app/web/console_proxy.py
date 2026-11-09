"""Pure helpers for the lab console reverse proxy (``app.web.labs``).

Kept separate from the route module so the security-relevant string handling
(management-address validation, path validation, cookie/redirect confinement)
is small, importable and testable on its own.
"""

# Ported at new-composition genesis (web layer wave).

from __future__ import annotations

import ipaddress
import re
from urllib.parse import quote, urljoin, urlsplit

import httpx
from fastapi import HTTPException, Request


def safe_mgmt_address(value: object) -> str | None:
    """Accept only a plain private IPv4 literal as a RouterOS management address.

    The value is persisted by the lab worker from containerlab inspect output,
    but it is interpolated into an upstream URL, so a malformed value (a host
    name, ``user@host``, a port, ``::1``, metadata/link-local, loopback) must
    never become an SSRF target.
    """
    try:
        ip = ipaddress.IPv4Address(str(value))
    except ValueError:
        return None
    if not ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast:
        return None
    return str(ip)


# Device cookies are namespaced so they can never collide with (or overwrite)
# Academy's ``session`` / ``csrf_token`` cookies, and scoped to the proxy path.
DEVICE_COOKIE_PREFIX = "wf_"
COOKIE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
COOKIE_VALUE_RE = re.compile(r"^[\x21\x23-\x2b\x2d-\x3a\x3c-\x5b\x5d-\x7e]{0,2048}$")
MAX_DEVICE_COOKIES = 8

_WEBFIG_ROOT = b"/webfig/"
_WEBFIG_SCRIPT_TYPES = frozenset({"application/javascript", "application/x-javascript"})


def console_upstream_timeout() -> httpx.Timeout:
    """Bound connection work but permit WebFig's indefinite notification poll."""
    return httpx.Timeout(30.0, read=None)


def should_rewrite_webfig_body(content_type: str | None) -> bool:
    """Return whether a response can contain browser-visible WebFig paths."""
    if content_type is None:
        return False
    media_type = content_type.partition(";")[0].strip().lower()
    return media_type.startswith("text/") or media_type in _WEBFIG_SCRIPT_TYPES


class WebFigRootRewriter:
    """Rewrite root-relative WebFig paths without buffering the response body."""

    def __init__(self, prefix: str) -> None:
        if not prefix.startswith("/") or prefix.endswith("/"):
            raise ValueError("console prefix must be an absolute path without a trailing slash")
        self._replacement = prefix.encode("utf-8") + _WEBFIG_ROOT
        self._pending = b""

    def feed(self, chunk: bytes) -> bytes:
        """Rewrite complete matches and retain only a possible split match."""
        data = self._pending + chunk
        output = bytearray()
        while True:
            match = data.find(_WEBFIG_ROOT)
            if match >= 0:
                output.extend(data[:match])
                output.extend(self._replacement)
                data = data[match + len(_WEBFIG_ROOT) :]
                continue

            keep = len(_WEBFIG_ROOT) - 1
            if len(data) > keep:
                output.extend(data[:-keep])
                data = data[-keep:]
            self._pending = data
            return bytes(output)

    def finish(self) -> bytes:
        """Return the final bytes once the upstream response is exhausted."""
        pending = self._pending
        self._pending = b""
        return pending


def safe_subpath(subpath: str) -> str:
    """Validate and percent-encode a console sub-resource path.

    ``subpath`` arrives already decoded, so a ``%2e%2e`` or ``%3F`` in the URL
    would otherwise become a literal ``..`` or ``?`` once concatenated onto the
    upstream URL. Reject traversal/oddities outright and re-encode the rest so
    the value can only ever be a path below the node's own root.
    """
    if any(ord(c) < 0x20 or ord(c) == 0x7F or c == "\\" for c in subpath):
        raise HTTPException(status_code=400, detail="invalid console path")
    if subpath.startswith("/") or "//" in subpath:
        raise HTTPException(status_code=400, detail="invalid console path")
    if any(seg in ("..", ".") for seg in subpath.split("/")):
        raise HTTPException(status_code=400, detail="invalid console path")
    return quote(subpath, safe="/:@!$&'()*+,;=-._~")


def device_cookie_header(request: Request) -> str | None:
    """Rebuild the Cookie header from device-namespaced cookies only."""
    pairs = []
    for name, value in request.cookies.items():
        if name.startswith(DEVICE_COOKIE_PREFIX) and COOKIE_VALUE_RE.match(value):
            real = name[len(DEVICE_COOKIE_PREFIX) :]
            if COOKIE_NAME_RE.match(real):
                pairs.append(f"{real}={value}")
    return "; ".join(pairs[:MAX_DEVICE_COOKIES]) or None


def rewrite_set_cookie(raw: str, prefix: str, secure: bool) -> str | None:
    """Re-issue a device cookie namespaced, path-scoped and HttpOnly, or drop it."""
    parts = [p.strip() for p in raw.split(";")]
    name, sep, value = parts[0].partition("=")
    name = name.strip()
    if not sep or not COOKIE_NAME_RE.match(name) or not COOKIE_VALUE_RE.match(value.strip()):
        return None
    out = f"{DEVICE_COOKIE_PREFIX}{name}={value.strip()}; Path={prefix}/; HttpOnly; SameSite=Strict"
    for attr in parts[1:]:
        key, _, val = attr.partition("=")
        if key.strip().lower() == "max-age" and re.fullmatch(r"-?\d{1,10}", val.strip()):
            out += f"; Max-Age={val.strip()}"
    return out + ("; Secure" if secure else "")


def rewrite_location(location: str, target: str, prefix: str, *, preserve_fragment: bool = False) -> str | None:
    """Keep a device redirect inside the proxy prefix; drop foreign redirects."""
    if any(ord(c) < 0x20 or ord(c) == 0x7F or c == "\\" for c in location):
        return None
    # Resolve relative and dot-segment redirects against the device URL first;
    # returning them unchanged would let ``../../`` escape the proxy prefix in
    # the learner's browser even though the upstream host itself was safe.
    try:
        base = urlsplit(target)
        loc = urlsplit(urljoin(target, location))
    except ValueError:
        return None
    if loc.scheme != base.scheme or loc.netloc != base.netloc:
        return None
    path = loc.path or "/"
    # Browsers normalize percent-encoded dot segments too, unlike urljoin.
    # Reject those before prefixing without decoding legitimate encoded slashes.
    if any(re.fullmatch(r"(?:\.|%2e){1,2}", segment, re.IGNORECASE) for segment in path.split("/")):
        return None
    out = prefix + (path if path.startswith("/") else f"/{path}")
    return (
        out
        + (f"?{loc.query}" if loc.query else "")
        + (f"#{loc.fragment}" if preserve_fragment and loc.fragment else "")
    )
