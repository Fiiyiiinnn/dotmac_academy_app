"""Maintained port of legacy_probe/test_bootstrap_policy_settings.py.

Issue #2 wave 4: DISPOSITIONS row -> test_bootstrap_policy_deny_pair_and_stored_null.
PlatformSetting fixtures port to kernel DomainSetting (public.domain_settings);
kernel storage forbids NULL-valued rows outright, so stored-NULL cases assert
the insert is refused. Shadow-schema cases now shadow ``domain_settings``.
"""

import pytest
from dotmac_kernel.settings_models import DomainSetting
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.config import validate_bootstrap_policy_value
from app.services.settings_store import effective, get_all, set_many


@pytest.fixture
def admin_session(genesis_database):
    admin, _superuser, _name = genesis_database
    db = sessionmaker(bind=admin, autoflush=False)()
    try:
        yield db
        db.rollback()
    finally:
        db.close()


def _row(db, key):
    return db.scalars(
        select(DomainSetting).where(
            DomainSetting.domain == "academy",
            DomainSetting.scope_kind == "platform",
            DomainSetting.key == key,
        )
    ).one_or_none()


@pytest.mark.parametrize("value", [None, "null", "[true]", '[["login"]]', '[["login",""]]'])
def test_bootstrap_role_pair_malformed_denied(value):
    with pytest.raises(ValueError):
        validate_bootstrap_policy_value("bootstrap_allowed_database_role_pairs", value)


def test_empty_role_pair_is_explicit_deny():
    assert validate_bootstrap_policy_value("bootstrap_allowed_database_role_pairs", "[]") == ()


def test_custom_configuration_preserves_arbitrary_valid_status(admin_session):
    set_many(
        admin_session,
        {
            "bootstrap_allowed_database_role_pairs": '[["operator","effective"]]',
            "bootstrap_role_provisioning": '{"founder":"Founding role"}',
            "bootstrap_initial_role_slug": "founder",
            "bootstrap_tenant_is_active": "false",
            "account_initial_is_active": "false",
            "account_initial_profile_status": "custom_review",
            "bootstrap_password_min_length": "19",
            "bootstrap_owner_first_name": "Founder",
            "bootstrap_owner_last_name_template": "{tenant_name}",
        },
    )
    configured = effective(admin_session.connection(), require_store=True)
    assert configured.bootstrap_allowed_database_role_pairs == (("operator", "effective"),)
    assert configured.bootstrap_role_provisioning == {"founder": "Founding role"}
    assert configured.bootstrap_initial_role_slug == "founder"
    assert configured.bootstrap_tenant_is_active is False
    assert configured.account_initial_is_active is False
    assert configured.account_initial_profile_status == "custom_review"
    assert configured.bootstrap_password_min_length == 19


def test_connection_and_session_projection_preserve_pending_semantics(admin_session, monkeypatch):
    db = admin_session
    monkeypatch.setattr(db, "autoflush", True)
    row = DomainSetting(
        tenant_id=None,
        scope_kind="platform",
        domain="academy",
        key="bootstrap_password_min_length",
        value_text="17",
        value_json=None,
    )
    db.add(row)
    db.flush()
    assert db.scalar(select(DomainSetting).where(DomainSetting.key == row.key)) is row
    db.execute(
        DomainSetting.__table__.update().where(DomainSetting.key == row.key).values(value_text="18"),
        execution_options={"synchronize_session": False},
    )
    assert row.value_text == "17"  # Retained ORM object must not mask actual scalar state.
    assert effective(db.connection(), require_store=True).bootstrap_password_min_length == 18
    row.value_text = "19"
    with db.no_autoflush:
        assert get_all(db)[row.key] == "18"
    assert get_all(db)[row.key] == "19"
    assert row.value_text == "19"
    db.delete(row)
    assert row.key not in get_all(db)


@pytest.mark.parametrize("key", ["bootstrap_allowed_database_role_pairs", "bootstrap_password_min_length"])
def test_bootstrap_policy_deny_pair_and_stored_null(admin_session, key):
    # Empty pair is an explicit deny, never a fallback.
    assert validate_bootstrap_policy_value("bootstrap_allowed_database_role_pairs", "[]") == ()
    set_many(admin_session, {"bootstrap_allowed_database_role_pairs": "[]"})
    assert effective(admin_session.connection(), require_store=True).bootstrap_allowed_database_role_pairs == ()
    admin_session.rollback()
    # Kernel storage forbids the NULL-valued row outright, so a stored NULL
    # bootstrap policy can never be hidden as an absent override.
    admin_session.add(
        DomainSetting(
            tenant_id=None, scope_kind="platform", domain="academy", key=key, value_text=None, value_json=None
        )
    )
    with pytest.raises(IntegrityError):
        admin_session.flush()
    admin_session.rollback()


@pytest.mark.parametrize("surface", ["session", "connection"])
def test_public_deny_settings_cannot_be_shadowed(admin_session, surface):
    from uuid import uuid4

    db = admin_session
    shadow = "settings_shadow_" + uuid4().hex
    db.execute(text(f"CREATE SCHEMA {shadow}"))
    db.execute(text(f"CREATE TABLE {shadow}.domain_settings (LIKE public.domain_settings INCLUDING ALL)"))
    set_many(db, {"bootstrap_allowed_database_role_pairs": "[]"})
    # Shadow row copies the public row's shape with a shadow value.
    db.execute(
        text(
            f"INSERT INTO {shadow}.domain_settings SELECT * FROM public.domain_settings "  # noqa: S608
            "WHERE key = 'bootstrap_allowed_database_role_pairs'"
        )
    )
    db.execute(
        text(
            f"UPDATE {shadow}.domain_settings "  # noqa: S608
            'SET value_json = \'[["shadow_login","shadow_effective"]]\'::json, value_text = NULL'
        )
    )
    db.execute(text(f"SET LOCAL search_path=pg_catalog,{shadow},public"))
    target = db if surface == "session" else db.connection()
    assert get_all(target)["bootstrap_allowed_database_role_pairs"] == []
    assert effective(target, require_store=True).bootstrap_allowed_database_role_pairs == ()


@pytest.mark.parametrize("surface", ["session", "connection"])
@pytest.mark.parametrize("translation", ["default", "public"])
def test_canonical_settings_ignore_schema_translation(admin_session, surface, translation):
    from uuid import uuid4

    db = admin_session
    shadow = "settings_translation_" + uuid4().hex
    db.execute(text(f"CREATE SCHEMA {shadow}"))
    db.execute(text(f"CREATE TABLE {shadow}.domain_settings (LIKE public.domain_settings INCLUDING ALL)"))
    set_many(db, {"bootstrap_password_min_length": "29"})
    db.execute(
        text(
            f"INSERT INTO {shadow}.domain_settings SELECT * FROM public.domain_settings "  # noqa: S608
            "WHERE key = 'bootstrap_password_min_length'"
        )
    )
    db.execute(text(f"UPDATE {shadow}.domain_settings SET value_text = '3'"))  # noqa: S608
    connection = db.connection()
    previous = connection.get_execution_options().get("schema_translate_map")
    try:
        connection.execution_options(schema_translate_map={None if translation == "default" else "public": shadow})
        target = db if surface == "session" else connection
        assert get_all(target)["bootstrap_password_min_length"] == "29"
        assert effective(target, require_store=True).bootstrap_password_min_length == 29
    finally:
        connection.execution_options(schema_translate_map=previous)
