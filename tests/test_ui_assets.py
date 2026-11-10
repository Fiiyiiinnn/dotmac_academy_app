"""The product stylesheet URL is content-fingerprinted from one place.

Manual `?v=` bumps drifted (shell, login, and lifecycle pages each
carried a different version), so returning browsers kept stale CSS on
exactly the email-linked pages. `app.ui` now derives the query from the
file bytes and every shell consumes that single URL.
"""

from __future__ import annotations

import re
from hashlib import sha256
from pathlib import Path

from app.ui import APP_CSS_URL, template_globals

ROOT = Path(__file__).resolve().parents[1]


def test_app_css_url_is_the_content_digest():
    expected = sha256((ROOT / "static" / "app.css").read_bytes()).hexdigest()[:12]
    assert APP_CSS_URL == f"/static/app.css?v={expected}"
    assert template_globals()["academy_app_css_url"] == APP_CSS_URL


def test_no_hardcoded_app_css_versions_remain():
    pattern = re.compile(r"app\.css\?v=\d")
    offenders = []
    for base in (ROOT / "templates", ROOT / "app"):
        for path in base.rglob("*"):
            if path.is_file() and path.suffix in {".html", ".py"}:
                if pattern.search(path.read_text(encoding="utf-8", errors="ignore")):
                    offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []
