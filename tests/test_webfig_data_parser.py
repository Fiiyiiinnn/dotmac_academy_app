"""Run the shipped browser parser contract in hosted CI's Node runtime."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.private_fixture_data


def _node_contract_succeeded(result, expected_tests):
    """Accept only a complete TAP workload; never return child diagnostics."""
    if result.returncode != 0 or expected_tests <= 0:
        return False
    expected = {
        "tests": expected_tests,
        "suites": 0,
        "pass": expected_tests,
        "fail": 0,
        "cancelled": 0,
        "skipped": 0,
        "todo": 0,
    }
    for field, count in expected.items():
        values = re.findall(rf"^# {field} (\d+)$", result.stdout, flags=re.MULTILINE)
        if values != [str(count)]:
            return False
    return True


def test_webfig_parser_under_disabled_string_code_generation():
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the WebFig parser contract"
    repo = Path(__file__).parents[1]
    result = subprocess.run(  # noqa: S603 - fixed repository-owned Node test entrypoint
        [node, "--test", "--test-reporter=tap", "tests/js/test_webfig_data_parser.cjs"],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    source = (repo / "tests/js/test_webfig_data_parser.cjs").read_text()
    expected_tests = len(re.findall(r'^test\("', source, flags=re.MULTILINE))
    succeeded = _node_contract_succeeded(result, expected_tests)
    assert succeeded, "Node WebFig contract incomplete or failed; private child diagnostics withheld"
