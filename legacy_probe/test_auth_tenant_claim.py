"""Auth tenant-claim canaries."""

from __future__ import annotations

from fastapi.testclient import TestClient

from legacy_probe.conftest import client_for


def test_public_self_registration_is_not_exposed(app_client: TestClient, tenant_a):
    a = client_for(app_client, tenant_a.slug)
    response = a.post(
        "/auth/register",
        json={
            "email": "anonymous@example.com",
            "password": "correct horse battery staple",
            "first_name": "Anonymous",
            "last_name": "User",
        },
    )
    assert response.status_code == 404


def test_jwt_issued_for_tenant_a_rejected_on_tenant_b(
    app_client: TestClient,
    tenant_a,
    tenant_b,
    api_actor,
):
    a = client_for(app_client, tenant_a.slug)
    token = api_actor(
        a,
        tenant_a,
        role="student",
        email="alice-auth@a.example.com",
    )["token"]

    assert a.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 200

    b = client_for(TestClient(app_client.app), tenant_b.slug)
    rejected = b.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert rejected.status_code == 401
