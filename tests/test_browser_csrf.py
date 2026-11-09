"""Browser CSRF injector contract using the shipped script in a Node VM."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_NODE_HARNESS = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const scriptPath = process.argv[1];
const cookie = process.argv[2];
const initialHeaders = JSON.parse(process.argv[3]);
let registeredEvent = null;
let handler = null;
const document = {
  cookie,
  body: {
    addEventListener(name, callback) {
      registeredEvent = name;
      handler = callback;
    }
  }
};
vm.runInNewContext(fs.readFileSync(scriptPath, 'utf8'), { document });
if (registeredEvent !== 'htmx:configRequest' || typeof handler !== 'function') {
  throw new Error('CSRF handler was not registered for htmx:configRequest');
}
const headers = { ...initialHeaders };
handler({ detail: { headers } });
process.stdout.write(JSON.stringify({ registeredEvent, headers }));
"""


@pytest.mark.parametrize(
    ("cookie", "initial_headers", "expected_headers"),
    [
        (
            "other=value; __Host-csrf_token=host-dummy; csrf_token=regular-dummy",
            {},
            {"x-csrf-token": "host-dummy"},
        ),
        (
            "csrf_token=regular-dummy",
            {},
            {"x-csrf-token": "regular-dummy"},
        ),
        ("other=value", {}, {}),
        (
            "__Host-csrf_token=host-dummy",
            {"X-CSRF-TOKEN": "explicit-dummy"},
            {"X-CSRF-TOKEN": "explicit-dummy"},
        ),
    ],
    ids=("host-cookie-preferred", "regular-cookie-supported", "missing-cookie", "explicit-header-kept"),
)
def test_shipped_csrf_script_sets_expected_htmx_header(cookie, initial_headers, expected_headers):
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the browser CSRF contract test"
    script_path = Path(__file__).parents[1] / "static" / "csrf.js"

    result = subprocess.run(  # noqa: S603 - fixed Node harness and dummy test values
        [node, "-e", _NODE_HARNESS, str(script_path), cookie, json.dumps(initial_headers)],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )

    observed = json.loads(result.stdout)
    assert observed["registeredEvent"] == "htmx:configRequest"
    assert observed["headers"] == expected_headers
