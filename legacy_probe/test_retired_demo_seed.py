"""Retired demo entrypoints must refuse without reading credentials or loading DB code."""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "seed_academy_demo.py"

# Preload only the harness machinery before installing guards. The target source
# is compiled first so every later file access (including a .env read) is denied.
PROBE = r"""
import importlib.abc
import os
import pathlib
import sys

code = compile(pathlib.Path(sys.argv[1]).read_text(), sys.argv[1], "exec")
mode = sys.argv[2]

class ForbiddenAccess(RuntimeError):
    pass

class NoRuntimeImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in {
            "app", "sqlalchemy", "dotmac_kernel", "dotmac_learning",
            "psycopg", "psycopg2", "dotenv",
        }:
            raise ForbiddenAccess("runtime import")

class NoEnvironment(dict):
    def __getitem__(self, key):
        raise ForbiddenAccess("environment read")
    def get(self, key, default=None):
        raise ForbiddenAccess("environment read")
    def __iter__(self):
        raise ForbiddenAccess("environment read")
    def __contains__(self, key):
        raise ForbiddenAccess("environment read")
    def keys(self):
        raise ForbiddenAccess("environment read")
    def items(self):
        raise ForbiddenAccess("environment read")
    def values(self):
        raise ForbiddenAccess("environment read")

def audit(event, args):
    if event in {"open", "socket.connect", "subprocess.Popen", "os.system"}:
        raise ForbiddenAccess("file or external access")

sys.meta_path.insert(0, NoRuntimeImports())
os.environ = NoEnvironment()
sys.addaudithook(audit)

if mode.startswith("control-"):
    try:
        if mode == "control-import":
            __import__("sqlalchemy")
        elif mode == "control-file":
            open(".env")
        elif mode == "control-environment":
            os.getenv("SEED_ADMIN_PASSWORD")
    except ForbiddenAccess:
        print("CONTROL_BLOCKED")
    else:
        raise AssertionError("guard failed to detect planted access")
else:
    namespace = {"__name__": "__main__" if mode == "script" else "scripts.seed_academy_demo"}
    if mode == "import":
        exec(code, namespace)
        print("IMPORT_COLD")
    else:
        try:
            exec(code, namespace)
            namespace["main"]()
        except SystemExit as exc:
            assert exc.code == 2, "retirement must refuse with exit status 2"
            print("REFUSED_COLD")
        else:
            raise AssertionError("retired entrypoint returned successfully")
"""


@pytest.mark.parametrize("mode", ["import", "main", "script"])
def test_retired_entrypoint_stays_cold(mode):
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-S", "-c", PROBE, str(SCRIPT), mode],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ("IMPORT_COLD" if mode == "import" else "REFUSED_COLD")
    if mode == "import":
        assert result.stderr == ""
    else:
        assert "bootstrap-tenant --help" in result.stderr
        assert "RETIRED_DEMO_SEED.md" in result.stderr


@pytest.mark.parametrize("mode", ["control-import", "control-file", "control-environment"])
def test_cold_probe_detects_planted_access(mode):
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-S", "-c", PROBE, str(SCRIPT), mode],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "CONTROL_BLOCKED"


@pytest.mark.parametrize("invocation", [[str(SCRIPT)], ["-m", "scripts.seed_academy_demo"]])
def test_real_invocation_refuses_and_never_echoes_supplied_secrets(invocation):
    sentinel = "synthetic-retirement-sentinel-42"
    result = subprocess.run(  # noqa: S603
        [sys.executable, *invocation, "--admin-password", sentinel],
        cwd=ROOT,
        env={**os.environ, "SEED_ADMIN_PASSWORD": sentinel},
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert "retired" in result.stderr
    assert "bootstrap-tenant --help" in result.stderr
    assert sentinel not in result.stderr


def test_no_hardcoded_password_default_in_scripts():
    pattern = re.compile(r"""environ\.get\(\s*["'][A-Z_]*PASSWORD[A-Z_]*["']\s*,\s*["']([^"']+)["']""")
    offenders = [
        str(path.relative_to(ROOT)) for path in (ROOT / "scripts").rglob("*.py") if pattern.search(path.read_text())
    ]
    assert offenders == [], f"hardcoded password defaults in scripts: {offenders}"
