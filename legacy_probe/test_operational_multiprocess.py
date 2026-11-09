"""Hosted genuine Prometheus mmap aggregation canary; no application fake clients."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_real_prometheus_multiprocess_http_and_probe_aggregation(tmp_path):
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PROMETHEUS_MULTIPROC_DIR": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1"}
    producer = """
from app.metrics import LAB_HOST_UP, LAB_WRONG_HOST_REFUSALS, observe_request
observe_request(method='GET', route='/canary/{id}', status_code=200, duration_seconds=0.02)
LAB_HOST_UP.set(1)
LAB_WRONG_HOST_REFUSALS.inc()
"""
    for _ in range(2):
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-c", producer], cwd=root, env=env,
            capture_output=True, timeout=60, check=False,
        )
        assert result.returncode == 0, "Prometheus producer failed; inspect securely in hosted CI"
    consumer = """
from app.metrics import PipelineSnapshot, render
snapshot = PipelineSnapshot(applicants=(('applied', 3),), labs=(('active', 2),), valid=1, invalid=0, capacity=4)
payload, _ = render(snapshot=snapshot)
assert b'academy_http_requests_total{method="GET",route="/canary/{id}",status="2xx"} 2.0' in payload
assert b'academy_http_request_duration_seconds_count{method="GET",route="/canary/{id}"} 2.0' in payload
assert b'academy_lab_host_up 1.0' in payload
assert b'academy_lab_wrong_host_refusals_total 2.0' in payload
assert b'academy_applicants{status="applied"} 3.0' in payload
without_pipeline, _ = render()
assert b'academy_applicants' not in without_pipeline
assert b'academy_lab_capacity' not in without_pipeline
"""
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", consumer], cwd=root, env=env,
        capture_output=True, timeout=60, check=False,
    )
    assert result.returncode == 0, "Prometheus aggregation canary failed; inspect securely in hosted CI"
