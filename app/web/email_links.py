"""Absolute links for emails and other off-site use.

``surface_path`` returns only a path, which a mail client cannot open. The link
origin is the raw ``Host`` header that tenant middleware already verified, and it
is re-checked here against the resolved tenant (its verified custom domain or
``<slug>.<platform root domain>``) before any link is built. ``X-Forwarded-*``
headers and ``scope["scheme"]`` never decide the host; in production the scheme is
always ``https`` from configuration.
"""

# Ported at new-composition genesis (web layer wave).

from __future__ import annotations

import re
from contextlib import AbstractContextManager
from urllib.parse import urlencode

from dotmac_kernel.config import settings
from dotmac_kernel.models import TenantDomain
from dotmac_kernel.session_runtime import resolve_database_runtime
from dotmac_kernel.web_surfaces import surface_path
from sqlalchemy import select
from sqlalchemy.orm import Session

# A plain hostname with an optional numeric port: no userinfo, path or whitespace.
_HOST = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?::[0-9]{1,5})?")


class LinkOriginRefused(RuntimeError):
    """The request host is not the resolved tenant's verified host; no link is built."""


def resolver_session() -> AbstractContextManager[Session]:
    """Use the public owner of the same runtime bound by tenant middleware."""
    return resolve_database_runtime().resolver_session()


def _has_verified_domain(tenant_id, host: str) -> bool:
    # The same resolver session tenant middleware uses; the caller's tenant-scoped
    # session is not what verifies a Host.
    with resolver_session() as resolver_db:
        return (
            resolver_db.scalar(
                select(TenantDomain.domain).where(
                    TenantDomain.tenant_id == tenant_id,
                    TenantDomain.domain == host,
                    TenantDomain.verified_at.is_not(None),
                )
            )
            is not None
        )


def _verified_origin(request) -> str:
    raw = request.headers.get("host", "")
    tenant = getattr(request.state, "tenant", None)
    if tenant is None or not _HOST.fullmatch(raw):
        raise LinkOriginRefused("link origin refused")
    host, _, port = raw.partition(":")
    host = host.lower()
    root = settings.platform_root_domain.lower().lstrip(".")
    on_subdomain = bool(root) and host == f"{tenant.slug}.{root}"
    if not (on_subdomain or _has_verified_domain(tenant.id, host)):
        raise LinkOriginRefused("link origin refused")
    if settings.is_production:
        return f"https://{host}"  # scheme from configuration, never from the request
    return f"{request.url.scheme}://{host}" + (f":{port}" if port else "")


def absolute_surface_link(request, local: str, **query: str) -> str:
    """Return ``<scheme>://<host><surface path>?<query>`` for a sibling route."""
    link = f"{_verified_origin(request)}{surface_path(request, local)}"
    return f"{link}?{urlencode(query)}" if query else link
