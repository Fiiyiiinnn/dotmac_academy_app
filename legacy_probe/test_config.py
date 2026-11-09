"""Production configuration fail-closed contracts."""

import pytest
from pydantic import ValidationError

from app.config import Settings, validate_settings


def _production_settings(**overrides):
    values = {
        "environment": "production",
        "database_url": "postgresql+psycopg://app_user@db/academy",
        "platform_database_url": "postgresql+psycopg://settings_writer@db/academy",
        "platform_root_domain": "academy.example.com",
        "tenancy": "single",
        "trusted_hosts": "academy.example.com,*.academy.example.com",
        "jwt_secret": "production-jwt-secret",
        "session_hash_secret": "production-session-secret",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_production_requires_single_tenancy():
    """The posture is still required in production; the mechanism moved.

    It used to be ACADEMY_TENANT_SLUG naming the tenant. It is now the kernel's
    TENANCY=single, which asserts at startup that exactly one tenant row exists
    — the identity comes from the database rather than from configuration.
    """
    errors = validate_settings(_production_settings(tenancy="multi"))
    assert any("TENANCY must be 'single' in production" in e for e in errors)

    assert not any("TENANCY" in e for e in validate_settings(_production_settings(tenancy="single")))


def test_lab_host_role_defaults_to_web():
    assert Settings(_env_file=None).lab_host_role == "web"


def test_lab_host_role_parses_environment(monkeypatch):
    monkeypatch.setenv("LAB_HOST_ROLE", "lab")
    assert Settings(_env_file=None).lab_host_role == "lab"


def test_lab_host_role_rejects_unknown_values():
    errors = validate_settings(_production_settings(lab_host_role="workerish"))
    assert "LAB_HOST_ROLE must be 'web' or 'lab'" in errors


def test_per_tenant_lab_limit_defaults_to_global_inheritance():
    assert Settings(_env_file=None).max_concurrent_labs_per_tenant == 0


def test_lab_capacity_limits_fail_closed_on_invalid_values():
    with pytest.raises(
        ValidationError, match="max_concurrent_labs must be an integer between 1 and 2147483647"
    ):
        _production_settings(max_concurrent_labs=0)
    with pytest.raises(
        ValidationError, match="max_concurrent_labs_per_tenant must be an integer between 0 and 2147483647"
    ):
        _production_settings(max_concurrent_labs_per_tenant=-1)


def test_lab_host_requires_a_dedicated_worker_dsn():
    missing = validate_settings(
        _production_settings(lab_host_role="lab", lab_worker_database_url="")
    )
    assert "LAB_WORKER_DATABASE_URL is required when LAB_HOST_ROLE=lab" in missing

    wrong_role = validate_settings(
        _production_settings(
            lab_host_role="lab",
            lab_worker_database_url="postgresql+psycopg://postgres@db/academy",
        )
    )
    assert "LAB_WORKER_DATABASE_URL must authenticate as academy_lab_worker" in wrong_role

    valid = validate_settings(
        _production_settings(
            lab_host_role="lab",
        lab_worker_database_url="postgresql+psycopg://academy_lab_worker@db/academy",
        )
    )
    assert not any("LAB_WORKER_DATABASE_URL" in error for error in valid)


def test_production_rejects_disabled_browser_guards():
    errors = validate_settings(
        _production_settings(
            csrf_enabled=False,
            rate_limit_enabled=False,
        )
    )
    assert "CSRF_ENABLED must be true in production" in errors
    assert "RATE_LIMIT_ENABLED must be true in production" in errors


def test_erp_integration_requires_companion_settings_and_distinct_secrets():
    errors = validate_settings(
        _production_settings(
            erp_inbound_hmac_secret="same-secret",
            erp_webhook_secret="same-secret",
            erp_assessment_token_secret="",
            erp_allowed_return_origins="",
            academy_public_base_url="",
        )
    )
    assert "ERP_ASSESSMENT_TOKEN_SECRET is required when ERP inbound integration is enabled" in errors
    assert "ERP_ALLOWED_RETURN_ORIGINS is required when ERP inbound integration is enabled" in errors
    assert "ACADEMY_PUBLIC_BASE_URL is required when ERP inbound integration is enabled" in errors
    assert "ERP inbound and outbound HMAC secrets must be distinct" in errors


def test_erp_public_base_url_requires_https_in_production():
    errors = validate_settings(_production_settings(academy_public_base_url="http://academy.example.com"))
    assert "ACADEMY_PUBLIC_BASE_URL must use HTTPS in production" in errors
