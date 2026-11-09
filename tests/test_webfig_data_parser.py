"""Run the shipped browser parser contract in hosted CI's Node runtime."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def test_webfig_parser_under_disabled_string_code_generation():
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the WebFig parser contract"
    repo = Path(__file__).parents[1]
    result = subprocess.run(  # noqa: S603 - fixed repository-owned Node test entrypoint
        [node, "--test", "tests/js/test_webfig_data_parser.cjs"],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
