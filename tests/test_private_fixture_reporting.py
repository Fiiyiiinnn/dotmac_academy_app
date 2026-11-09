"""Hosted subprocess disclosure regressions using exclusively synthetic data."""

from __future__ import annotations

import os
import runpy
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SENTINEL = "NONSECRET_SYNTHETIC_VENDOR_DISCLOSURE_SENTINEL"


def _run_pytest(tmp_path, source, *options):
    shutil.copyfile(ROOT / "conftest.py", tmp_path / "conftest.py")
    (tmp_path / "test_synthetic.py").write_text(source)
    return subprocess.run(  # noqa: S603 - fixed interpreter, isolated synthetic test source
        [sys.executable, "-m", "pytest", "-q", "--junitxml=report.xml", *options],
        cwd=tmp_path,
        env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": ""},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.mark.parametrize("phase", ["setup", "call", "teardown", "passing"])
def test_marked_reports_withhold_synthetic_payload_in_terminal_and_junit(tmp_path, phase):
    source = f"""
import logging
import os
import sys
import warnings
import pytest

PAYLOAD = {SENTINEL!r}

def disclose():
    print(PAYLOAD)
    print(PAYLOAD, file=sys.stderr)
    os.write(1, PAYLOAD.encode())
    os.write(2, PAYLOAD.encode())
    logging.warning(PAYLOAD)
    warnings.warn(PAYLOAD)

@pytest.fixture
def private_fixture(request):
    disclose()
    request.node.user_properties.append(("private_payload", PAYLOAD))
    if {phase!r} == "setup":
        raise RuntimeError(PAYLOAD)
    yield
    disclose()
    if {phase!r} == "teardown":
        raise RuntimeError(PAYLOAD)

@pytest.mark.private_fixture_data
def test_private_case(private_fixture):
    disclose()
    if {phase!r} == "call":
        assert PAYLOAD == "different"

def test_public_case():
    assert False, "ordinary public failure remains visible"
"""
    result = _run_pytest(
        tmp_path, source, "-o", "junit_logging=all", "-o", "junit_log_passing_tests=true", "--showlocals"
    )
    terminal = result.stdout + result.stderr
    junit = (tmp_path / "report.xml").read_text()
    assert result.returncode == 1
    assert SENTINEL not in terminal
    assert SENTINEL not in junit
    assert "ordinary public failure remains visible" in terminal
    assert "ordinary public failure remains visible" in junit
    # Trusted output from our isolated synthetic subprocess, with no fixture data.
    root = ET.fromstring(junit)  # noqa: S314
    cases = {case.attrib["name"]: case for case in root.iter("testcase")}
    assert set(cases) == {"test_private_case", "test_public_case"}
    private_errors = list(cases["test_private_case"].iter("error"))
    private_failures = list(cases["test_private_case"].iter("failure"))
    assert len(private_errors) == int(phase in {"setup", "teardown"})
    assert len(private_failures) == int(phase == "call")
    if phase != "passing":
        assert "test_private_case" in terminal
        assert "diagnostics withheld" in terminal
        exception_type = "AssertionError" if phase == "call" else "RuntimeError"
        assert exception_type in terminal


@pytest.mark.parametrize(
    "options",
    [
        ["-s"],
        ["--capture=sys"],
        ["--capture=tee-sys"],
        ["--log-cli-level=INFO"],
        ["-o", "log_cli=true"],
        ["--log-file=unsafe.log"],
    ],
)
def test_unsafe_capture_and_logging_refused_before_payload_collection(tmp_path, options):
    source = f"""
from pathlib import Path
print({SENTINEL!r})
Path("payload_executed").write_text("yes")
def test_private_case():
    pass
"""
    result = _run_pytest(tmp_path, source, *options)
    assert result.returncode == 4
    assert SENTINEL not in result.stdout + result.stderr
    assert "Private-fixture reporting" in result.stdout + result.stderr
    assert not (tmp_path / "payload_executed").exists()


def _node_summary(**overrides):
    counts = {"tests": 11, "suites": 0, "pass": 11, "fail": 0, "cancelled": 0, "skipped": 0, "todo": 0}
    counts.update(overrides)
    return "\n".join(f"# {field} {value}" for field, value in counts.items())


@pytest.mark.parametrize(
    ("stdout", "returncode", "expected", "accepted"),
    [
        (_node_summary(), 0, 11, True),
        ("", 0, 11, False),
        (_node_summary(), 1, 11, False),
        (_node_summary(tests=0, **{"pass": 0}), 0, 0, False),
        (_node_summary(tests=10), 0, 11, False),
        (_node_summary(skipped=1), 0, 11, False),
        (_node_summary(cancelled=1), 0, 11, False),
        (_node_summary(todo=1), 0, 11, False),
        (_node_summary(suites=1), 0, 11, False),
        (_node_summary(fail=1), 0, 11, False),
        (_node_summary() + "\n# tests 11", 0, 11, False),
        (_node_summary().replace("# tests 11", "# tests invalid"), 0, 11, False),
    ],
)
def test_node_workload_requires_complete_nonempty_success(stdout, returncode, expected, accepted):
    wrapper = runpy.run_path(str(ROOT / "tests/test_webfig_data_parser.py"))
    result = SimpleNamespace(stdout=stdout, stderr=SENTINEL, returncode=returncode)
    assert wrapper["_node_contract_succeeded"](result, expected) is accepted


def test_node_wrapper_withholds_child_failure_diagnostics(monkeypatch):
    wrapper = runpy.run_path(str(ROOT / "tests/test_webfig_data_parser.py"))
    monkeypatch.setattr(wrapper["shutil"], "which", lambda name: "/synthetic/node")
    calls = []

    def failed_child(*args, **kwargs):
        calls.append(args[0])
        return SimpleNamespace(stdout=SENTINEL, stderr=SENTINEL, returncode=1)

    monkeypatch.setattr(wrapper["subprocess"], "run", failed_child)
    with pytest.raises(AssertionError) as failure:
        wrapper["test_webfig_parser_under_disabled_string_code_generation"]()
    assert SENTINEL not in str(failure.value)
    assert "private child diagnostics withheld" in str(failure.value)
    assert len(calls) == 1
    assert "--test-reporter=tap" in calls[0]
