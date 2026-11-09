"""Anonymous `/` renders the public landing (ADR 0003).

Historical note: Task 11 made `/` an auth-gated dashboard redirecting
visitors to /login; ADR 0003 replaced that redirect with the public landing.
Signed-in behaviour (Learn Home) is pinned in tests/web/test_public_catalog.py.
"""


def test_landing_renders_public_page_for_anonymous(app_client, tenant_a):
    # tenant_a ensures the 'alpha' tenant exists so TenantResolverMiddleware resolves it.
    r = app_client.get("/", headers={"Host": "alpha.localhost"}, follow_redirects=False)
    assert r.status_code == 200
    assert "Browse the courses" in r.text
