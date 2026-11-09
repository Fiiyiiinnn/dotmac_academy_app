"""Historical metrics canaries adapted to the tenant/read-only snapshot contract.

Authored for hosted PostgreSQL and genuine installed owners; not run in source capsule.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError

from app.config import Settings, settings
from legacy_probe.conftest import client_for


def test_metrics_gate_performs_no_endpoint_work(app_client, tenant_a, monkeypatch):
    import app.operational as route

    def forbidden(*args, **kwargs):
        raise AssertionError("denied metrics executed endpoint work")

    monkeypatch.setattr(route, "require_tenant", forbidden)
    monkeypatch.setattr(route, "refresh_lab_host_probe", forbidden)
    monkeypatch.setattr(route, "refresh_pipeline_metrics", forbidden)
    monkeypatch.setattr(route, "render", forbidden)
    monkeypatch.setattr(settings, "metrics_token", "scrape-policy")
    client = client_for(TestClient(app_client.app), tenant_a.slug)
    for authorization in (None, "wrong", "scrape-policy", "Basic scrape-policy", "Bearer", "Bearer wrong", "Bearer scrape-policy extra"):
        headers = {} if authorization is None else {"Authorization": authorization}
        assert client.get("/metrics", headers=headers).status_code == 404
    monkeypatch.setattr(settings, "metrics_token", "")
    assert client.get("/metrics", headers={"Authorization": "Bearer scrape-policy"}).status_code == 404


def test_matching_bearer_and_route_template(app_client, tenant_a, monkeypatch):
    monkeypatch.setattr(settings, "metrics_token", "scrape-policy")
    client = client_for(TestClient(app_client.app), tenant_a.slug)
    client.get("/health")
    client.get("/no-such-page-cardinality-canary")
    response = client.get("/metrics", headers={"Authorization": "bEaReR scrape-policy"})
    assert response.status_code == 200
    assert 'route="/health"' in response.text
    assert 'route="(unmatched)"' in response.text
    assert "cardinality-canary" not in response.text
    assert "academy_http_requests_total" in response.text


def test_utf8_token_and_missing_tenant(monkeypatch):
    from starlette.requests import Request

    from app.operational import metrics

    monkeypatch.setattr(settings, "metrics_token", "unicode-é")
    request = Request({"type": "http", "headers": [(b"authorization", "Bearer unicode-é".encode("latin-1"))]})
    # Starlette decodes HTTP header bytes as latin-1. Comparison must use bytes,
    # not compare_digest(str, str), which raises for non-ASCII credentials.
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as error:
        metrics(request)
    assert error.value.status_code == 404


def test_probe_policy_cache_destination_and_expiry(monkeypatch):
    import app.metrics as metrics

    calls = []
    clock = [100.0]

    class Sock:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def connect(address, timeout):
        calls.append((address, timeout))
        return Sock()

    monkeypatch.setattr(metrics, "_last_probe", None)
    monkeypatch.setattr(metrics.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(metrics.socket, "create_connection", connect)
    metrics.refresh_lab_host_probe("host-a", 10)
    metrics.refresh_lab_host_probe("host-a", 10)
    assert len(calls) == 1
    clock[0] += settings.metrics_lab_probe_ttl_seconds
    metrics.refresh_lab_host_probe("host-a", 10)
    assert len(calls) == 2
    metrics.refresh_lab_host_probe("host-b", 10)
    metrics.refresh_lab_host_probe("host-b", 11)
    monkeypatch.setattr(settings, "metrics_lab_probe_timeout_seconds", 0.5)
    metrics.refresh_lab_host_probe("host-b", 11)
    monkeypatch.setattr(settings, "metrics_lab_probe_ttl_seconds", 0.25)
    metrics.refresh_lab_host_probe("host-b", 11)
    assert len(calls) == 6
    assert calls[-1] == (("host-b", 11), 0.5)


@pytest.mark.parametrize("key", ["metrics_lab_probe_ttl_seconds", "metrics_lab_probe_timeout_seconds", "metrics_pipeline_ttl_seconds"])
@pytest.mark.parametrize("value", [True, False, 0, -1, float("inf"), float("nan"), "infinity", "", None, []])
def test_metrics_durations_refuse_invalid(key, value):
    with pytest.raises(ValueError):
        Settings(**{key: value})


def test_bounded_methods_and_historical_protocol():
    import app.metrics as metrics

    for i in range(20):
        metrics.observe_request(method=f"SCANNER{i}", route="(unmatched)", status_code=999, duration_seconds=0.01)
    payload, _ = metrics.render()
    assert b'SCANNER' not in payload
    assert b'method="OTHER"' in payload
    assert b'status="other"' in payload
    assert metrics.LAB_WRONG_HOST_REFUSALS._name == "academy_lab_wrong_host_refusals"
    assert metrics.HTTP_DURATION._upper_bounds[:-1] == [0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0]


def test_genuine_pipeline_query_budget_tenant_scope_readonly_and_context_failure(app_client, tenant_a, tenant_b, admin_session, monkeypatch):
    from dotmac_kernel.session_runtime import resolve_database_runtime

    import app.metrics as metrics
    from app.models.admissions import APPLICANT_STATUSES, Applicant
    from app.services import settings_store

    # Use real rows for both tenants; no substitute session/collector establishes
    # the positive isolation/read-only result.
    for tenant, amount in ((tenant_a, 2), (tenant_b, 5)):
        for _ in range(amount):
            admin_session.add(Applicant(tenant_id=tenant.id, email=f"{uuid4()}@metrics.invalid", first_name="Metrics", last_name="Canary", status="waitlisted", assessment_taken_at=datetime.now(UTC), assessment_valid=True))
    admin_session.commit()
    runtime = resolve_database_runtime()
    statements = []
    scopes = []
    capacity_calls = []
    original_effective = settings_store.effective
    clock = [100.0]
    original_readonly = type(runtime).readonly_session

    def observe(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lower())

    def effective(db):
        capacity_calls.append(True)
        scopes.append(db.execute(text("SELECT current_setting('app.current_tenant'), current_setting('transaction_read_only')")).one())
        return original_effective(db)

    event.listen(runtime.engine, "before_cursor_execute", observe)
    monkeypatch.setattr(settings_store, "effective", effective)
    monkeypatch.setattr(metrics, "_last_pipeline", None)
    monkeypatch.setattr(metrics.time, "monotonic", lambda: clock[0])
    try:
        first = metrics.refresh_pipeline_metrics(tenant_a.id)
        assert first is not None
        assert dict(first.applicants)["waitlisted"] == 2
        assert set(dict(first.applicants)) == set(APPLICANT_STATUSES)
        assert first.valid == 2
        assert scopes == [(str(tenant_a.id), "on")]
        assert len(capacity_calls) == 1
        assert sum("group by" in sql for sql in statements) == 3
        before = len(statements)
        assert metrics.refresh_pipeline_metrics(tenant_a.id) is first
        assert len(statements) == before
        clock[0] += settings.metrics_pipeline_ttl_seconds
        second = metrics.refresh_pipeline_metrics(tenant_b.id)
        assert second is not None
        assert dict(second.applicants)["waitlisted"] == 5
        assert len(capacity_calls) == 2
        monkeypatch.setattr(settings, "metrics_pipeline_ttl_seconds", 0.5)
        assert metrics.refresh_pipeline_metrics(tenant_b.id) is not second
        assert len(capacity_calls) == 3

        @contextmanager
        def unavailable(self):
            raise RuntimeError("fixed failure")
            yield

        monkeypatch.setattr(type(runtime), "readonly_session", unavailable)
        clock[0] += 1
        assert metrics.refresh_pipeline_metrics(tenant_b.id) is not None
        failed = metrics.refresh_pipeline_metrics(tenant_a.id)
        assert failed is None
        payload, _ = metrics.render(snapshot=failed)
        assert b"academy_applicants" not in payload
        assert b"academy_lab_instances" not in payload
        assert b"academy_lab_capacity" not in payload
        # An already returned immutable snapshot is still safe to render while
        # another request replaces/fails the one-entry cache.
        payload, _ = metrics.render(snapshot=first)
        assert b'academy_applicants{status="waitlisted"} 2.0' in payload
    finally:
        event.remove(runtime.engine, "before_cursor_execute", observe)
    monkeypatch.setattr(type(runtime), "readonly_session", original_readonly)
    with pytest.raises(DBAPIError) as error, runtime.readonly_session() as db, runtime.tenant_scope(db, tenant_a.id):
        db.execute(text("UPDATE applicants SET status = status WHERE tenant_id = :tenant"), {"tenant": tenant_a.id})
    assert getattr(error.value.orig, "sqlstate", None) == "25006"


_MULTIPROC_WORKER = """
import sys
from app import metrics
metrics.LAB_HOST_UP.set(float(sys.argv[1]))
"""


def _collect_multiproc(tmp_path):
    from prometheus_client import CollectorRegistry, multiprocess

    registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(registry, path=str(tmp_path))
    return {(s.name, tuple(sorted(s.labels.items()))): s.value for fam in registry.collect() for s in fam.samples}


def test_pipeline_counts_never_come_from_worker_files_across_processes(tmp_path):
    """Composed equivalent of MAIN's mostrecent DB-gauge test (#183).

    Pipeline families are deliberately absent from the process/mmap registry:
    no worker can write ``academy_lab_instances`` into the multiproc files, so
    a stale worker cannot pin the series. The only cross-process value is the
    probe gauge (``max``: one reaching worker is enough). The queued count is
    rendered per request from the caller's snapshot, exactly as supplied.
    """
    import os
    import subprocess
    import sys

    import app.metrics as metrics

    env = {**os.environ, "PROMETHEUS_MULTIPROC_DIR": str(tmp_path)}
    for host_up in ("1", "0"):
        subprocess.run([sys.executable, "-c", _MULTIPROC_WORKER, host_up], env=env, check=True)  # noqa: S603

    got = _collect_multiproc(tmp_path)
    assert got[("academy_lab_host_up", ())] == 1.0
    assert not any(name == "academy_lab_instances" for name, _ in got)

    snapshot = metrics.PipelineSnapshot(
        applicants=(("applied", 1),),
        labs=(("queued", 3), ("active", 1)),
        valid=0,
        invalid=0,
        capacity=4,
    )
    payload, _ = metrics.render(snapshot=snapshot)
    assert b'academy_lab_instances{status="queued"} 3.0' in payload
    # Exactly one queued series: the snapshot value, never a worker-file duplicate.
    assert payload.count(b'academy_lab_instances{status="queued"}') == 1
