"""Prometheus metrics — bounded HTTP instrumentation for the observe stack.

Cardinality is deliberately bounded (the cross-Dotmac metrics-safety rule):
the route label is the MATCHED route template (``/courses/{slug}``), never the
raw path, and unmatched requests collapse into a single ``(unmatched)`` bucket
so scanners cannot mint label values.

Gunicorn runs multiple workers, each with its own process registry, so prod
must set ``PROMETHEUS_MULTIPROC_DIR`` (a per-boot scratch dir, e.g. a systemd
``RuntimeDirectory``); the client library then aggregates across workers at
scrape time. Without the env var (dev, tests, single process) the default
in-process registry is used.

The ``/metrics`` endpoint itself lives in ``app.operational`` and is inert unless
``METRICS_TOKEN`` is configured — the app is on the public internet, and
operational metrics are internal.
"""

# Ported at new-composition genesis (composition wave).

from __future__ import annotations

import logging
import os
import socket
import time
from dataclasses import dataclass
from threading import Lock
from uuid import UUID

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    multiprocess,
)
from prometheus_client.core import GaugeMetricFamily

from app.config import settings

HTTP_REQUESTS = Counter(
    "academy_http_requests_total",
    "HTTP requests by method, matched route template, and status class.",
    ("method", "route", "status"),
)

HTTP_DURATION = Histogram(
    "academy_http_request_duration_seconds",
    "HTTP request duration by matched route template.",
    ("method", "route"),
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)


# Labs run on a separate KVM host reached over a private link (WireGuard). If
# that link drops, provisioning still succeeds but every console 502s — a
# silent failure for students. This gauge makes it alertable.
# multiprocess_mode="max": each gunicorn worker probes independently; one
# worker reaching the lab host is enough to call the link up.
LAB_HOST_UP = Gauge(
    "academy_lab_host_up",
    "1 when the lab host answers on its console/tunnel address, else 0.",
    multiprocess_mode="max",
)

LAB_WRONG_HOST_REFUSALS = Counter(
    "academy_lab_wrong_host_refusals_total",
    "Lab engine calls refused because this process is not on the declared lab host.",
)

LAB_RESULT_REFUSALS = Counter(
    "academy_lab_result_refusals_total",
    "Fenced lab result calls refused, grouped by a bounded SQLSTATE category.",
    ("sqlstate",),
)


# Pipeline series are deliberately absent from the process/mmap registry. They
# belong to the authenticated request's snapshot, never a previous tenant.
@dataclass(frozen=True)
class PipelineSnapshot:
    applicants: tuple[tuple[str, int], ...]
    labs: tuple[tuple[str, int], ...]
    valid: int
    invalid: int
    capacity: int


_probe_lock = Lock()
_pipeline_lock = Lock()
_last_probe: tuple[tuple[object, ...], float, float] | None = None
_last_pipeline: tuple[tuple[object, ...], float, PipelineSnapshot] | None = None
logger = logging.getLogger(__name__)
_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "CONNECT", "TRACE"})
_LAB_STATUSES = ("queued", "provisioning", "resetting", "active", "error", "reaped")


def observe_request(*, method: str, route: str, status_code: int, duration_seconds: float) -> None:
    method = method if method in _METHODS else "OTHER"
    status = f"{status_code // 100}xx" if 100 <= status_code <= 599 else "other"
    HTTP_REQUESTS.labels(method=method, route=route, status=status).inc()
    HTTP_DURATION.labels(method=method, route=route).observe(duration_seconds)


def refresh_lab_host_probe(host: str, port: int) -> None:
    """One bounded cache entry, keyed by destination and process probe policy."""
    global _last_probe
    ttl = settings.metrics_lab_probe_ttl_seconds
    timeout = settings.metrics_lab_probe_timeout_seconds
    context = (host, port, timeout, ttl)
    with _probe_lock:
        now = time.monotonic()
        if _last_probe is not None and _last_probe[0] == context and now - _last_probe[1] < ttl:
            LAB_HOST_UP.set(_last_probe[2])
            return
        try:
            with socket.create_connection((host, port), timeout=timeout):
                value = 1.0
        except Exception:
            logger.debug("lab host probe failed")
            value = 0.0
        _last_probe = (context, time.monotonic(), value)
        LAB_HOST_UP.set(value)


def refresh_pipeline_metrics(tenant_id: UUID) -> PipelineSnapshot | None:
    """Refresh three grouped counts and one capacity read in a tenant read-only transaction.

    The cache holds only one context. A failed refresh may reuse its own stale
    snapshot; another context's snapshot is never returned or rendered.
    """
    global _last_pipeline
    ttl = settings.metrics_pipeline_ttl_seconds
    context = (tenant_id, ttl)
    with _pipeline_lock:
        now = time.monotonic()
        previous = _last_pipeline if _last_pipeline is not None and _last_pipeline[0] == context else None
        if previous is not None and now - previous[1] < ttl:
            return previous[2]
        try:
            from dotmac_kernel.session_runtime import resolve_database_runtime
            from sqlalchemy import func, select

            from app.models.admissions import APPLICANT_STATUSES, Applicant
            from app.models.lab import LabInstance
            from app.services.settings_store import effective

            runtime = resolve_database_runtime()
            with runtime.readonly_session() as db, runtime.tenant_scope(db, tenant_id):
                counts: dict[str, int] = dict(
                    db.execute(
                        select(Applicant.status, func.count())
                        .where(Applicant.tenant_id == tenant_id)
                        .group_by(Applicant.status)
                    )
                    .tuples()
                    .all()
                )
                labs: dict[str, int] = dict(
                    db.execute(
                        select(LabInstance.status, func.count())
                        .where(LabInstance.tenant_id == tenant_id)
                        .group_by(LabInstance.status)
                    )
                    .tuples()
                    .all()
                )
                sittings: dict[bool | None, int] = dict(
                    db.execute(
                        select(Applicant.assessment_valid, func.count())
                        .where(Applicant.tenant_id == tenant_id, Applicant.assessment_taken_at.is_not(None))
                        .group_by(Applicant.assessment_valid)
                    )
                    .tuples()
                    .all()
                )
                capacity = int(effective(db).max_concurrent_labs)
                candidate = PipelineSnapshot(
                    applicants=tuple((status, int(counts.get(status, 0))) for status in APPLICANT_STATUSES),
                    labs=tuple((status, int(labs.get(status, 0))) for status in _LAB_STATUSES),
                    valid=int(sittings.get(True, 0)),
                    invalid=int(sittings.get(False, 0)) + int(sittings.get(None, 0)),
                    capacity=capacity,
                )
            _last_pipeline = (context, time.monotonic(), candidate)
            return candidate
        except Exception:
            logger.debug("pipeline metrics refresh failed")
            return previous[2] if previous is not None else None


class _PipelineCollector:
    def __init__(self, snapshot: PipelineSnapshot) -> None:
        self.snapshot = snapshot

    def collect(self):
        snapshot = self.snapshot
        for name, description, label, values in (
            ("academy_applicants", "Applicants by admissions pipeline status.", "status", snapshot.applicants),
            ("academy_lab_instances", "Lab instances by lifecycle status.", "status", snapshot.labs),
            (
                "academy_entrance_sittings",
                "Graded entrance sittings by validity ('valid', 'invalid').",
                "validity",
                (("valid", snapshot.valid), ("invalid", snapshot.invalid)),
            ),
        ):
            family = GaugeMetricFamily(name, description, labels=[label])
            for key, value in values:
                family.add_metric([key], value)
            yield family
        yield GaugeMetricFamily(
            "academy_lab_capacity",
            "Configured maximum concurrent lab instances.",
            value=snapshot.capacity,
        )


def render(*, snapshot: PipelineSnapshot | None = None) -> tuple[bytes, str]:
    """Aggregate actual process metrics and append only this request's pipeline."""
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
    else:
        registry = REGISTRY
    payload = generate_latest(registry)
    if snapshot is not None:
        pipeline = CollectorRegistry()
        pipeline.register(_PipelineCollector(snapshot))
        payload += generate_latest(pipeline)
    return payload, CONTENT_TYPE_LATEST
