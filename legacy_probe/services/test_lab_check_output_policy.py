"""Authored canaries for configurable learner output, not executed locally."""

from unittest.mock import MagicMock

import pytest

from app.config import Settings, validate_lab_check_output_limit
from app.services.checks.engine import run_checks
from app.services.labengine.interface import ExecResult


@pytest.mark.parametrize("key", ["lab_check_evidence_chars", "lab_check_dns_actual_chars"])
@pytest.mark.parametrize("raw", [None, "", " ", True, False, 0, -1, 1.5, "1.0", "-2", "nan", float("inf")])
def test_output_policy_refuses_malformed_and_empty_values(key, raw):
    with pytest.raises(ValueError):
        Settings(_env_file=None, **{key: raw})
    with pytest.raises(ValueError):
        validate_lab_check_output_limit(raw)


def test_output_policy_environment_and_original_defaults(monkeypatch):
    monkeypatch.delenv("LAB_CHECK_EVIDENCE_CHARS", raising=False)
    monkeypatch.delenv("LAB_CHECK_DNS_ACTUAL_CHARS", raising=False)
    defaults = Settings(_env_file=None)
    assert defaults.lab_check_evidence_chars == 160
    assert defaults.lab_check_dns_actual_chars == 120
    monkeypatch.setenv("LAB_CHECK_EVIDENCE_CHARS", "7")
    monkeypatch.setenv("LAB_CHECK_DNS_ACTUAL_CHARS", "4")
    configured = Settings(_env_file=None)
    assert configured.lab_check_evidence_chars == 7
    assert configured.lab_check_dns_actual_chars == 4


def test_nondefault_limits_change_results_without_changing_grading(monkeypatch):
    from app.services.checks import primitives

    configured = Settings(_env_file=None, lab_check_evidence_chars=7, lab_check_dns_actual_chars=4)
    monkeypatch.setattr(primitives, "settings", configured)
    engine = MagicMock()
    engine.exec.return_value = ExecResult("Address example.net", "", 1)
    checks = [
        {"id": "command", "type": "command", "node": "n", "command": "dump"},
        {"id": "dns", "type": "probe", "node": "n", "probe": {"kind": "dns", "target": "example.net"}},
    ]
    out = run_checks(checks, engine, object(), {})
    assert out["score"] == 1 and out["max_score"] == 2
    assert out["per_check"][0]["detail"].endswith("Output was: Address…")
    assert out["per_check"][1]["actual"] == "Addr"
    assert out["per_check"][1]["pass"] is True


def test_evidence_boundary_whitespace_and_empty_output(monkeypatch):
    from app.services.checks import primitives

    monkeypatch.setattr(primitives, "settings", Settings(_env_file=None, lab_check_evidence_chars=1))
    assert primitives._evidence(" \n \t") == "no output"
    assert primitives._evidence(" x \n") == "x"
    assert primitives._evidence("x y") == "x…"


def test_seed_bounds_remain_authored_inputs():
    from app.services.lab_seed import generate_seed

    assert generate_seed({"x": {"type": "int", "min": -9, "max": -9}}, 1) == {"x": -9}
    for entry in ({"type": "int", "max": 4}, {"type": "int", "min": 4}):
        with pytest.raises(KeyError):
            generate_seed({"x": entry}, 1)
    with pytest.raises(IndexError):
        generate_seed({"x": {"type": "choice", "options": []}}, 1)
