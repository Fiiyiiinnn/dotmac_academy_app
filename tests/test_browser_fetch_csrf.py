"""Direct-fetch CSRF cookie contract using helpers from the shipped scripts."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_NODE_HARNESS = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const cookie = process.argv[2];
const helper = source.match(/function csrfToken\(\) \{[\s\S]*?\n\s*\}/);
if (!helper) throw new Error('csrfToken helper not found in shipped script');
const token = vm.runInNewContext('(' + helper[0] + ')()', { document: { cookie } });
process.stdout.write(JSON.stringify({ token }));
"""

_REPO = Path(__file__).parents[1]
_SCRIPTS = ("static/chapter.js", "static/apply_assessment.js")


@pytest.mark.parametrize("script", _SCRIPTS)
@pytest.mark.parametrize(
    ("cookie", "expected"),
    [
        ("other=value; __Host-csrf_token=host-dummy; csrf_token=regular-dummy", "host-dummy"),
        ("csrf_token=regular-dummy", "regular-dummy"),
        ("other=value", None),
    ],
    ids=("host-cookie-preferred", "regular-cookie-fallback", "missing-cookie"),
)
def test_shipped_fetch_script_reads_supported_csrf_cookie(script, cookie, expected):
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the browser fetch CSRF contract test"

    result = subprocess.run(  # noqa: S603 - fixed Node harness and dummy test values
        [node, "-e", _NODE_HARNESS, str(_REPO / script), cookie],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert json.loads(result.stdout)["token"] == expected


def test_direct_fetches_remain_same_origin():
    chapter = (_REPO / "static/chapter.js").read_text()
    assessment = (_REPO / "static/apply_assessment.js").read_text()

    assert 'credentials: "same-origin"' in chapter
    assert '"/courses/" + encodeURIComponent(courseSlug)' in chapter
    assert "fetch('/apply/assessment/save'" in assessment
