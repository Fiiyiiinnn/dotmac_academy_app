"""Cross-tenant isolation canaries.

These tests are the load-bearing invariant for the whole architecture. If they fail,
something is wrong at the routing, application, or RLS layer — and the failure mode
is "data leak between customers", which is unacceptable.

Every new tenant-scoped table MUST add a parallel test in this file (or sibling).

The tests run against a real Postgres because RLS doesn't exist in SQLite. Set
`TEST_DATABASE_URL` to a disposable Postgres before running.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from legacy_probe.conftest import client_for


def test_person_created_in_tenant_a_invisible_to_tenant_b(
    app_client: TestClient,
    tenant_a,
    tenant_b,
    api_actor,
):
    a = client_for(app_client, tenant_a.slug)
    auth_a = api_actor(a, tenant_a)["headers"]
    resp = a.post(
        "/people",
        json={"email": "alice@a.example.com", "first_name": "Alice", "last_name": "A"},
        headers=auth_a,
    )
    assert resp.status_code == 201, resp.text
    person_id = resp.json()["id"]

    # From tenant B's subdomain, GET by exact ID must 404.
    b = client_for(TestClient(app_client.app), tenant_b.slug)
    auth_b = api_actor(b, tenant_b)["headers"]
    assert b.get(f"/people/{person_id}", headers=auth_b).status_code == 404
    # And listing must not include the person.
    listing = b.get("/people", headers=auth_b).json()
    assert person_id not in [p["id"] for p in listing]


def test_person_delete_from_other_tenant_returns_404(
    app_client: TestClient,
    tenant_a,
    tenant_b,
    api_actor,
):
    a = client_for(app_client, tenant_a.slug)
    auth_a = api_actor(a, tenant_a)["headers"]
    resp = a.post(
        "/people",
        json={"email": "bob@a.example.com", "first_name": "Bob", "last_name": "B"},
        headers=auth_a,
    )
    assert resp.status_code == 201
    person_id = resp.json()["id"]

    # Delete from tenant B context — must 404.
    b = client_for(TestClient(app_client.app), tenant_b.slug)
    auth_b = api_actor(b, tenant_b)["headers"]
    assert b.delete(f"/people/{person_id}", headers=auth_b).status_code == 404

    # Person still exists in tenant A.
    a2 = client_for(TestClient(app_client.app), tenant_a.slug)
    assert a2.get(f"/people/{person_id}", headers=auth_a).status_code == 200


def test_email_can_be_reused_across_tenants(
    app_client: TestClient,
    tenant_a,
    tenant_b,
    api_actor,
):
    """Same email in two tenants is two distinct people — see ADR D1."""
    a = client_for(app_client, tenant_a.slug)
    auth_a = api_actor(a, tenant_a)["headers"]
    r1 = a.post(
        "/people",
        json={"email": "shared@x.example.com", "first_name": "A", "last_name": "User"},
        headers=auth_a,
    )
    assert r1.status_code == 201

    b = client_for(TestClient(app_client.app), tenant_b.slug)
    auth_b = api_actor(b, tenant_b)["headers"]
    r2 = b.post(
        "/people",
        json={"email": "shared@x.example.com", "first_name": "B", "last_name": "User"},
        headers=auth_b,
    )
    assert r2.status_code == 201

    # And they have different IDs.
    assert r1.json()["id"] != r2.json()["id"]


def test_people_management_requires_admin_auth(app_client: TestClient, tenant_a):
    a = client_for(app_client, tenant_a.slug)
    assert a.get("/people").status_code == 401
    a.cookies.clear()
    assert (
        a.post(
            "/people",
            json={"email": "anonymous@example.com", "first_name": "Anon", "last_name": "User"},
        ).status_code
        == 401
    )
