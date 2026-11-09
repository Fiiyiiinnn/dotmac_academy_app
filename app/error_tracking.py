"""GlitchTip (Sentry-protocol) error tracking — off unless a DSN is configured.

Mirrors the dotmac_sub convention: the SDK is optional at runtime and the whole
subsystem is inert when ``GLITCHTIP_DSN`` is empty or ``GLITCHTIP_ENABLED`` is
false, so a dev box or an unconfigured deployment behaves exactly as before.

PII is not sent by default: applicant and student records are personal data,
and a crash report is not a lawful basis for shipping it to another system.
A canonical UUID request id may cross the egress boundary as a correlation tag.
Raw request fields and exception values are omitted from outbound events.
"""

# Ported at new-composition genesis (composition wave).

from __future__ import annotations

import logging
import math
import re
from typing import TYPE_CHECKING, cast
from uuid import UUID

from app.config import settings

if TYPE_CHECKING:
    from sentry_sdk._types import Event, Hint

logger = logging.getLogger(__name__)


def _project_event(event: dict, hint: dict) -> dict:
    """Narrow SDK egress boundary: omit all unlisted payload-bearing fields.

    Keep SDK identifiers, time, severity, exception type and source locations.
    Requests, messages, exception values, SQL, breadcrumbs, locals, code context,
    user/extra/tags and unknown future SDK fields never cross this seam.
    Trace context retains only SDK IDs and a fixed operation.
    """
    hint.pop("attachments", None)
    projected: dict = {"platform": "python"}
    for key in ("event_id", "trace_id", "span_id", "parent_span_id"):
        value = event.get(key)
        if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{16}|[0-9a-f]{32}", value):
            projected[key] = value
    for key in ("timestamp", "start_timestamp"):
        value = event.get(key)
        if type(value) in (int, float) and math.isfinite(cast(int | float, value)):
            projected[key] = value
        elif isinstance(value, str) and re.fullmatch(r"[0-9T:.+Z-]{10,40}", value):
            projected[key] = value
    if event.get("level") in ("debug", "info", "warning", "error", "fatal"):
        projected["level"] = event["level"]
    contexts = event.get("contexts")
    if isinstance(contexts, dict) and isinstance(contexts.get("trace"), dict):
        trace = _project_span(contexts["trace"])
        projected["contexts"] = {"trace": trace}
    tags = event.get("tags")
    request_id = tags.get("request_id") if isinstance(tags, dict) else None
    if isinstance(request_id, str):
        try:
            projected["tags"] = {"request_id": str(UUID(request_id))}
        except ValueError:
            pass
    exception = event.get("exception")
    if isinstance(exception, dict):
        values = []
        for value in exception.get("values", ()):
            if not isinstance(value, dict):
                continue
            safe = {}
            if isinstance(value.get("type"), str):
                safe["type"] = value["type"]
            frames = []
            stacktrace = value.get("stacktrace")
            if isinstance(stacktrace, dict):
                for frame in stacktrace.get("frames", ()):
                    if not isinstance(frame, dict):
                        continue
                    source = {key: frame[key] for key in ("module", "function") if isinstance(frame.get(key), str)}
                    if type(frame.get("lineno")) is int:
                        source["lineno"] = frame["lineno"]
                    frames.append(source)
            if frames:
                safe["stacktrace"] = {"frames": frames}
            values.append(safe)
        projected["exception"] = {"values": values}
    if event.get("type") == "transaction":
        projected["type"] = "transaction"
        projected["transaction"] = "academy.request"
        projected["transaction_info"] = {"source": "custom"}
        # Span descriptions and data include SQL, URLs and arguments. Retain
        # only timing/IDs, preserving configured tracing without those values.
        projected["spans"] = [_project_span(span) for span in event.get("spans", ()) if isinstance(span, dict)]
    return projected


def _project_span(span: dict) -> dict:
    safe = _project_event(
        {
            key: span[key]
            for key in (
                "trace_id",
                "span_id",
                "parent_span_id",
                "timestamp",
                "start_timestamp",
            )
            if key in span
        },
        {},
    )
    safe.pop("platform", None)
    safe["op"] = "academy.operation"
    return safe


def init_error_tracking(*, server_name: str) -> bool:
    """Initialise error tracking. Returns True when it was actually enabled."""
    if not settings.glitchtip_enabled or not settings.glitchtip_dsn:
        return False
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration
        from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
    except ImportError:
        logger.warning("sentry-sdk not installed; error tracking disabled")
        return False

    def project(event: Event, hint: Hint) -> Event:
        result = _project_event(cast("dict[str, object]", event), hint)
        result["server_name"] = server_name
        # The pinned SDK serializes before this callback and likewise types
        # that serialized JSON payload as Event (client._prepare_event).
        return cast("Event", result)

    try:
        sentry_sdk.init(
            dsn=settings.glitchtip_dsn,
            environment=settings.environment,
            traces_sample_rate=settings.glitchtip_traces_sample_rate,
            server_name=server_name,
            integrations=[
                FastApiIntegration(transaction_style="endpoint"),
                SqlalchemyIntegration(),
                LoggingIntegration(level=logging.WARNING, event_level=logging.ERROR),
            ],
            send_default_pii=False,
            max_request_body_size="never",
            include_local_variables=False,
            include_source_context=False,
            before_send=project,
            before_send_transaction=project,
        )
    except Exception:  # never let telemetry setup break startup
        logger.warning("failed to configure GlitchTip")
        return False
    logger.info("GlitchTip error tracking enabled")
    return True


def tag_request(request_id: str) -> None:
    """Tag the current scope with the request id (joins issues to Loki logs)."""
    if not settings.glitchtip_enabled or not settings.glitchtip_dsn:
        return
    try:
        import sentry_sdk

        sentry_sdk.set_tag("request_id", request_id)
    except Exception:  # pragma: no cover - telemetry must never raise
        logger.debug("failed to tag request id")
