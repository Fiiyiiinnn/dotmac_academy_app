"""Academy-owned operational routes.

The kernel owns generic liveness at ``/health``. Academy owns the metrics it
emits about its learning, admissions, and lab pipelines, so that endpoint stays
with the product and is mounted through the Academy feature manifest.
"""

# Ported at new-composition genesis (composition wave).

from __future__ import annotations

import hmac

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from app.api.deps import require_tenant
from app.config import settings
from app.metrics import refresh_lab_host_probe, refresh_pipeline_metrics, render

router = APIRouter()


@router.get("/metrics", include_in_schema=False)
def metrics(request: Request) -> Response:
    """Render bearer-gated Prometheus metrics, or appear absent when disabled."""
    authorization = (request.headers.get("authorization") or "").split()
    token = settings.metrics_token
    if (
        not token
        or len(authorization) != 2
        or authorization[0].lower() != "bearer"
        or not hmac.compare_digest(authorization[1].encode("utf-8"), token.encode("utf-8"))
    ):
        return JSONResponse(status_code=404, content={"detail": "Not Found"})

    tenant = require_tenant(request)
    refresh_lab_host_probe(settings.lab_console_host, settings.lab_host_probe_port)
    snapshot = refresh_pipeline_metrics(tenant.id)
    payload, content_type = render(snapshot=snapshot)
    return Response(content=payload, media_type=content_type)


__all__ = ["router"]
