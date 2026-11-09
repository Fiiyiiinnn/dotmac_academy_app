"""Shared web response helpers.

Keeps the htmx-vs-full-page redirect decision in one place so the ~17 routes
that navigate after a POST don't each re-implement (and drift on) it.
"""

# Ported at new-composition genesis (web layer wave).

from __future__ import annotations

from fastapi import Response, status
from fastapi.responses import RedirectResponse
from starlette.requests import Request


def hx_redirect(
    request: Request,
    target: str,
    *,
    status_code: int = status.HTTP_303_SEE_OTHER,
    hx_status: int = 200,
) -> Response:
    """Navigate to ``target`` for both htmx and full-page callers.

    htmx callers get an empty ``hx_status`` response carrying an ``HX-Redirect``
    header (htmx performs the client-side navigation); everyone else gets a
    normal ``status_code`` redirect. Callers that must also set a cookie can do
    so on the returned response (e.g. login → ``hx_status=204``).
    """
    if request.headers.get("HX-Request"):
        resp = Response(status_code=hx_status)
        resp.headers["HX-Redirect"] = target
        return resp
    return RedirectResponse(target, status_code=status_code)
