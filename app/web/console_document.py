"""Reviewed ttyd document identity; changes require a fresh asset/CSP review."""

from __future__ import annotations

import hashlib
from uuid import UUID

import httpx
from dotmac_kernel.facet_principal import FacetPrincipalError, require_facet_principal
from dotmac_kernel.web_surfaces import BrowserSecurityPlane
from fastapi import HTTPException, Request

from app.services.identity import AcademyCurrentAccount

TTYD_DOCUMENT_BYTES = 701420
TTYD_DOCUMENT_SHA256 = "065fa93ecafdd9e5c8bb687b9c59e8aa3c292518b20b4e896abb858b2ae67fe8"
TTYD_INLINE_SCRIPT_SHA256 = "8LQi6B6bPNRwZSx8ASb5pBEP32q4tpI2ZjNAn5S3oi0="


def require_console_principal(request: Request, person: AcademyCurrentAccount, tenant_id: UUID) -> None:
    """Academy's current-account ID is the canonical Party ID, not PartyPerson."""
    try:
        principal = require_facet_principal(request, plane=BrowserSecurityPlane.TENANT)
    except FacetPrincipalError as exc:
        raise HTTPException(status_code=403, detail="console principal mismatch") from exc
    if (
        principal.facet != "staff_admin"
        or principal.subject_id != person.id
        or principal.tenant_id != tenant_id
        or person.tenant_id != tenant_id
    ):
        raise HTTPException(status_code=403, detail="console principal mismatch")


async def read_reviewed_document(upstream: httpx.Response) -> bytes:
    """Buffer unencoded HTML before response start, capped at the reviewed length."""
    if upstream.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "text/html":
        raise HTTPException(status_code=502, detail="unreviewed console document")
    if upstream.headers.get("content-encoding", "").strip().lower() not in ("", "identity"):
        raise HTTPException(status_code=502, detail="unreviewed console encoding")
    document = bytearray()
    async for chunk in upstream.aiter_raw(chunk_size=min(64 * 1024, TTYD_DOCUMENT_BYTES + 1)):
        if len(document) + len(chunk) > TTYD_DOCUMENT_BYTES:
            raise HTTPException(status_code=502, detail="unreviewed console document")
        document.extend(chunk)
    if len(document) != TTYD_DOCUMENT_BYTES or hashlib.sha256(document).hexdigest() != TTYD_DOCUMENT_SHA256:
        raise HTTPException(status_code=502, detail="unreviewed console document")
    return bytes(document)
