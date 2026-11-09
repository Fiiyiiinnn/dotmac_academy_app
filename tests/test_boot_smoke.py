"""Hosted boot smoke: the composed app serves health + login.

Runs in a subprocess so product settings bind to the genesis database
exactly as a deployment would (env-first); the tenant resolves by Host
subdomain (`{slug}.localhost`), the same pattern the legacy suite uses.
Asserts the kernel health endpoint and the Academy login page answer 200.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]

BOOT = """
from fastapi.testclient import TestClient
from app.main import app

with TestClient(app, raise_server_exceptions=False, base_url="http://SLUG.localhost") as client:
    health = client.get("/health")
    print("health", health.status_code)
    login = client.get("/login")
    print("login", login.status_code)
    console = client.get(
        "/labs/instances/00000000-0000-0000-0000-000000000001/console/linux",
        follow_redirects=False,
    )
    assert console.status_code in (302, 303, 401, 403), console.text[:300]
    assert "8LQi6B6bPNRwZSx8ASb5pBEP32q4tpI2ZjNAn5S3oi0=" not in console.headers.get(
        "content-security-policy", ""
    )
assert health.status_code == 200, health.text[:300]
assert login.status_code == 200, login.text[:300]
assert "login" in login.text.lower()
print("BOOT-OK")
"""


def test_app_boots_and_serves_login(genesis_database):
    _admin, superuser, name = genesis_database
    admin_url = make_url(superuser).set(database=name).render_as_string(hide_password=False)
    engine = create_engine(admin_url)
    slug = f"boot{uuid.uuid4().hex[:8]}"
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO tenants (id, slug, name) VALUES (gen_random_uuid(), :s, 'Boot Tenant')"),
            {"s": slug},
        )
    engine.dispose()
    user_url = (
        make_url(superuser).set(database=name, username="app_user", password=None).render_as_string(hide_password=False)
    )
    env = {
        **os.environ,
        "DATABASE_URL": user_url,
        "PLATFORM_DATABASE_URL": user_url,
        "ENVIRONMENT": "dev",
        "CSRF_ENABLED": "true",
        "RATE_LIMIT_ENABLED": "true",
    }
    result = subprocess.run(  # noqa: S603 - fixed interpreter + constant script; slug is uuid-derived
        [sys.executable, "-c", BOOT.replace("SLUG", slug)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    assert "BOOT-OK" in result.stdout
