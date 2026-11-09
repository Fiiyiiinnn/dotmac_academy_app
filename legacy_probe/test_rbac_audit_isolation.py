"""RBAC and audit isolation canaries."""

from __future__ import annotations

from fastapi.testclient import TestClient

from legacy_probe.conftest import client_for


def test_cross_tenant_role_assignment_returns_404(
    app_client: TestClient,
    tenant_a,
    tenant_b,
    api_actor,
):
    a = client_for(app_client, tenant_a.slug)
    a_token = api_actor(
        a,
        tenant_a,
        email="admin-a@rbac.example.com",
    )["token"]
    role_id = _create_role(a, a_token, "support")["id"]

    b = client_for(TestClient(app_client.app), tenant_b.slug)
    b_person_id = str(
        api_actor(
            b,
            tenant_b,
            role="student",
            email="user-b@rbac.example.com",
        )["person"].id
    )

    response = a.post(
        "/rbac/role-grants",
        headers={"Authorization": f"Bearer {a_token}"},
        json={"person_id": b_person_id, "role_id": role_id},
    )
    assert response.status_code == 404


def test_audit_events_from_tenant_a_invisible_to_tenant_b(
    app_client: TestClient,
    tenant_a,
    tenant_b,
    api_actor,
):
    a = client_for(app_client, tenant_a.slug)
    a_token = api_actor(a, tenant_a, email="audit-a@rbac.example.com")["token"]
    _create_role(a, a_token, "audited-role")

    a_events = a.get("/rbac/audit-events", headers={"Authorization": f"Bearer {a_token}"})
    assert a_events.status_code == 200
    assert [event["action"] for event in a_events.json()] == ["role.create"]

    b = client_for(TestClient(app_client.app), tenant_b.slug)
    b_token = api_actor(b, tenant_b, email="audit-b@rbac.example.com")["token"]
    b_events = b.get("/rbac/audit-events", headers={"Authorization": f"Bearer {b_token}"})
    assert b_events.status_code == 200
    assert b_events.json() == []


def _create_role(client: TestClient, token: str, slug: str) -> dict[str, object]:
    response = client.post(
        "/rbac/roles",
        headers={"Authorization": f"Bearer {token}"},
        json={"slug": slug, "name": slug.replace("-", " ").title()},
    )
    assert response.status_code == 201, response.text
    return response.json()
