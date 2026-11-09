"""Actual composed browser onboarding/CSRF canaries; hosted only, NOT RUN."""

from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models.email_outbox import EmailOutbox
from legacy_probe.conftest import client_for, csrf_for
from legacy_probe.services.test_canonical_onboarding import _snapshot, onboarding_case

assert onboarding_case


@pytest.mark.parametrize("csrf", ["missing", "mismatch"])
def test_csrf_refusal_has_no_task_effect(app_client, onboarding_case, csrf):
    c = onboarding_case
    web = client_for(TestClient(app_client.app), c.a.tenant.slug)
    assert web.get(f"/onboarding?token={c.raw}").status_code == 200
    before = _snapshot(c)
    response = web.post(
        "/onboarding/orientation",
        data={"token": c.raw},
        headers={} if csrf == "missing" else {"x-csrf-token": "mismatch"},
    )
    assert response.status_code == 403
    assert _snapshot(c) == before


@pytest.mark.parametrize("htmx", [False, True])
def test_genuine_get_csrf_completion_redirect_and_actual_queued_invitation(app_client, onboarding_case, htmx):
    c = onboarding_case
    web = client_for(TestClient(app_client.app), c.a.tenant.slug)
    page = web.get(f"/onboarding?token={c.raw}")
    assert page.status_code == 200
    csrf = web.cookies.get("csrf_token")
    assert csrf
    headers = {"x-csrf-token": csrf}
    if htmx:
        headers["HX-Request"] = "true"
    response = web.post("/onboarding/orientation", data={"token": c.raw}, headers=headers, follow_redirects=False)
    assert response.status_code == (204 if htmx else 303)
    assert response.headers["HX-Redirect" if htmx else "location"] == f"/onboarding?token={c.raw}"
    c.a.db.expire_all()
    welcome = c.a.db.scalars(
        select(EmailOutbox).where(EmailOutbox.recipient == c.email, EmailOutbox.kind == "enrollment_welcome")
    ).one()
    setup = next(line for line in welcome.text_body.splitlines() if "/accept-invite?token=" in line)
    raw = parse_qs(urlparse(setup).query)["token"][0]
    assert raw and raw != c.raw
    # Genuine claim/login is a future33 installed-owner prerequisite. This test
    # intentionally reaches the actual composed claim surface without re-mint.
    fresh = client_for(TestClient(app_client.app), c.a.tenant.slug)
    # Follow the emailed link by path so the issued CSRF cookie lands on the
    # same host the POST uses (the link's absolute host is not the test base).
    link = urlparse(setup)
    # Pin the emailed host: it must be the tenant host this client addresses.
    assert (link.scheme, link.netloc) == (urlparse(str(fresh.base_url)).scheme, fresh.headers["Host"])
    assert fresh.get(f"{link.path}?{link.query}").status_code == 200
    response = fresh.post(
        "/accept-invite",
        data={"token": raw, "password": "chosen-password9"},
        headers={"x-csrf-token": csrf_for(fresh, {}, f"{link.path}?{link.query}")},
    )
    assert response.status_code == 200, response.text
    response = fresh.post("/auth/login", json={"email": c.email, "password": "chosen-password9"})
    assert response.status_code == 200, response.text
