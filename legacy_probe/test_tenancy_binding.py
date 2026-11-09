"""The single-tenant lockdown must actually be armed, not merely configured.

This exists because it once wasn't. ``TENANCY=single`` was set, but Academy
hand-built its lifespan and therefore never ran the kernel assertion. These
tests start Academy's real ASGI app (``app.main.app``, built by ``create_app``)
and let the kernel lifespan read the real tenant table, so reading the setting
without adopting the behavior cannot recur.

Each case runs in a FRESH subprocess. The kernel binding is process-global and
the database runtime is sealed once per process (kernel a107 refuses to rebind
``dotmac_kernel.db.resolver_session``), so an in-process monkeypatch of the
resolver no longer reaches the check. The tenants are real committed rows
(``tenant_a``/``tenant_b`` fixtures); nothing about the tenancy check itself is
replaced. Only unrelated configuration validation is stubbed in the child, as
the original in-process test did, so a production-mode boot reaches the check. The
``_product_startup_errors`` stub is NEW (the original did not have it) and is applied only to the
production case: production turns any product configuration error into a fatal boot error that
would mask the tenancy verdict, whereas a development boot only logs it.

A control that is configured but not armed is worse than one that is absent:
the configuration is evidence that someone thought about it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import text

_ROOT = Path(__file__).resolve().parent.parent

_PROBE = """
import json, sys
sys.path.insert(0, {root!r})
from dotmac_kernel.config import settings as kernel_settings

kernel_settings.tenancy = {mode!r}
kernel_settings.environment = {environment!r}
kernel_settings.seed_on_startup = False

import dotmac_kernel.app_factory as factory
factory.validate_settings = lambda _settings: []
factory._required_setting_errors = lambda: []
if {environment!r} == "production":
    factory._product_startup_errors = lambda _spec: []

from fastapi.testclient import TestClient
from dotmac_kernel.tenancy import single_tenant_binding
from app.main import app

outcome = {{"during": None, "error": None}}
try:
    with TestClient(app):
        outcome["during"] = single_tenant_binding()
except RuntimeError as exc:
    outcome["error"] = str(exc)
outcome["after"] = single_tenant_binding()
print("OUTCOME " + json.dumps(outcome))
"""


def _boot(mode: str, environment: str) -> dict:
    result = subprocess.run(  # noqa: S603 - fixed argv, own interpreter
        [sys.executable, "-c", _PROBE.format(root=str(_ROOT), mode=mode, environment=environment)],
        capture_output=True,
        text=True,
        cwd=_ROOT,
        env=os.environ.copy(),
        timeout=180,
    )
    # Never echo child stderr; at most the final line's exception class name.
    last = (result.stderr.strip().splitlines() or [""])[-1]
    failure = last.split(":", 1)[0].strip()[:80]
    assert result.returncode == 0, f"probe failed: {failure}"
    lines = [line for line in result.stdout.splitlines() if line.startswith("OUTCOME ")]
    assert len(lines) == 1, "probe printed no outcome"
    return json.loads(lines[0][len("OUTCOME "):])


def _tenant_slugs(admin_session) -> list[str]:
    admin_session.rollback()
    return sorted(admin_session.execute(text("SELECT slug FROM tenants")).scalars().all())


def test_single_tenancy_binds_at_startup(admin_session, tenant_a) -> None:
    """The property the outage lacked: after startup, the binding is set."""
    slugs = _tenant_slugs(admin_session)
    assert slugs == ["alpha"], f"foreign tenant rows leaked into the test database: {sorted(set(slugs) - {'alpha'})}"
    outcome = _boot("single", "development")
    assert outcome["during"] == "alpha"
    assert outcome["error"] is None


def test_single_tenancy_with_two_tenants_outside_production_binds_nothing_and_does_not_fail(
    admin_session, tenant_a, tenant_b
) -> None:
    """Outside production the check only logs: no binding is picked and the boot continues."""
    assert {"alpha", "beta"} <= set(_tenant_slugs(admin_session))
    outcome = _boot("single", "development")
    assert outcome["during"] is None
    assert outcome["error"] is None
    assert outcome["after"] is None


def test_multi_tenancy_binds_nothing(admin_session, tenant_a) -> None:
    outcome = _boot("multi", "development")
    assert outcome == {"during": None, "error": None, "after": None}


def test_two_tenants_is_fatal_and_binds_nothing(admin_session, tenant_a, tenant_b) -> None:
    """Refusing to boot is the point; binding one of two would pick a winner."""
    slugs = _tenant_slugs(admin_session)
    assert {"alpha", "beta"} <= set(slugs)
    outcome = _boot("single", "production")
    assert outcome["error"] is not None, "a production boot with several tenants must refuse"
    assert outcome["error"].startswith("Tenancy error")
    for slug in slugs:
        assert slug in outcome["error"], f"the refusal must name {slug}"
    assert outcome["during"] is None
    assert outcome["after"] is None
