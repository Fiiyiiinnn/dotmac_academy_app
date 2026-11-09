"""The product assembly, not test setup, registers the kernel display specs."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

_PROBE = """
import sys
sys.path.insert(0, {root!r})
import app.assembly  # noqa: F401  (the product assembly alone; no conftest)
from dotmac_kernel.settings_models import SettingDomain
from dotmac_kernel.settings_resolver import get_spec

expected = {{"timezone": "UTC", "date_format": "%Y-%m-%d", "datetime_format": "%Y-%m-%d %H:%M"}}
for key, default in expected.items():
    spec = get_spec(SettingDomain.display, key)
    assert spec.default == default, key
    assert spec.validator is not None, key
print("display-specs-ok")
"""


def test_product_assembly_registers_display_specs_in_a_clean_process():
    result = subprocess.run(  # noqa: S603 - fixed argv, own interpreter
        [sys.executable, "-c", _PROBE.format(root=str(_ROOT))],
        capture_output=True,
        text=True,
        cwd=_ROOT,
        timeout=120,
    )
    # Never echo child stderr; at most the final line's exception class name.
    last = (result.stderr.strip().splitlines() or [""])[-1]
    failure = last.split(":", 1)[0].strip()[:80]
    assert result.returncode == 0, f"probe failed: {failure}"
    assert "display-specs-ok" in result.stdout


def test_display_spec_validators_reject_bad_values():
    import pytest

    from app.display_settings import DISPLAY_SPECS

    by_key = {spec.key: spec for spec in DISPLAY_SPECS}
    assert set(by_key) == {"timezone", "date_format", "datetime_format"}
    with pytest.raises(ValueError):
        by_key["timezone"].validator("Not/AZone")
    for key in ("date_format", "datetime_format"):
        with pytest.raises(ValueError):
            by_key[key].validator("no directive")
