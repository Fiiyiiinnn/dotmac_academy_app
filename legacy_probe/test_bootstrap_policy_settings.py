"""Source-first configured bootstrap policy canaries, authored NOT RUN."""

import pytest
from sqlalchemy import select

from app.config import validate_bootstrap_policy_value
from app.models.platform_settings import PlatformSetting
from app.services.settings_store import effective, get_all


@pytest.mark.parametrize("value", [None, "null", "[true]", "[[\"login\"]]", "[[\"login\",\"\"]]"])
def test_bootstrap_role_pair_malformed_denied(value):
    with pytest.raises(ValueError):
        validate_bootstrap_policy_value("bootstrap_allowed_database_role_pairs", value)


def test_empty_role_pair_is_explicit_deny():
    assert validate_bootstrap_policy_value("bootstrap_allowed_database_role_pairs", "[]") == ()


def test_custom_configuration_preserves_arbitrary_valid_status(admin_session):
    from app.services.settings_store import set_many

    set_many(admin_session, {"bootstrap_allowed_database_role_pairs": '[["operator","effective"]]',
                             "bootstrap_role_provisioning": '{"founder":"Founding role"}',
                             "bootstrap_initial_role_slug": "founder", "bootstrap_tenant_is_active": "false",
                             "account_initial_is_active": "false", "account_initial_profile_status": "custom_review",
                             "bootstrap_password_min_length": "19", "bootstrap_owner_first_name": "Founder",
                             "bootstrap_owner_last_name_template": "{tenant_name}"})
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
    monkeypatch.setattr(db, 'autoflush', True)
    row = PlatformSetting(key="bootstrap_password_min_length", value="17")
    db.add(row)
    db.flush()
    assert db.scalar(select(PlatformSetting).where(PlatformSetting.key == row.key)) is row
    db.execute(PlatformSetting.__table__.update().where(PlatformSetting.key == row.key).values(value="18"),
               execution_options={"synchronize_session": False})
    assert row.value == "17"  # Retained ORM object must not mask actual scalar state.
    assert effective(db.connection(), require_store=True).bootstrap_password_min_length == 18
    row.value = "19"
    with db.no_autoflush:
        assert get_all(db)[row.key] == "18"
    assert get_all(db)[row.key] == "19"
    assert row.value == "19"
    db.delete(row)
    assert row.key not in get_all(db)


@pytest.mark.parametrize('key', ['bootstrap_allowed_database_role_pairs', 'bootstrap_password_min_length'])
def test_stored_null_bootstrap_policy_is_not_hidden(admin_session, key):
    admin_session.add(PlatformSetting(key=key,value=None))
    admin_session.flush()
    assert get_all(admin_session.connection())[key] is None
    with pytest.raises(ValueError):
        effective(admin_session.connection(),require_store=True)


@pytest.mark.parametrize('surface', ['session', 'connection'])
def test_public_deny_settings_cannot_be_shadowed(admin_session, surface):
    from uuid import uuid4

    from sqlalchemy import text

    db = admin_session
    shadow = 'settings_shadow_' + uuid4().hex
    db.execute(text(f'CREATE SCHEMA {shadow}'))
    db.execute(text(f'CREATE TABLE {shadow}.platform_settings (LIKE public.platform_settings INCLUDING ALL)'))
    db.execute(text("INSERT INTO public.platform_settings (key,value) VALUES "
                    "('bootstrap_allowed_database_role_pairs','[]') "
                    "ON CONFLICT (key) DO UPDATE SET value='[]'"))
    # Identifier is generated only from a constant prefix and uuid4.hex.
    db.execute(text(f"INSERT INTO {shadow}.platform_settings (key,value) VALUES "  # noqa: S608
                    "('bootstrap_allowed_database_role_pairs','[[\"shadow_login\",\"shadow_effective\"]]')"))
    db.execute(text(f'SET LOCAL search_path=pg_catalog,{shadow},public'))
    target = db if surface == 'session' else db.connection()
    assert get_all(target)['bootstrap_allowed_database_role_pairs'] == '[]'
    assert effective(target, require_store=True).bootstrap_allowed_database_role_pairs == ()


@pytest.mark.parametrize('surface', ['session', 'connection'])
@pytest.mark.parametrize('translation', ['default', 'public'])
def test_canonical_settings_ignore_schema_translation(admin_session, surface, translation):
    from uuid import uuid4

    from sqlalchemy import text

    db = admin_session
    shadow = 'settings_translation_' + uuid4().hex
    db.execute(text(f'CREATE SCHEMA {shadow}'))
    db.execute(text(f'CREATE TABLE {shadow}.platform_settings (LIKE public.platform_settings INCLUDING ALL)'))
    db.execute(text("INSERT INTO public.platform_settings (key,value) VALUES "
                    "('bootstrap_password_min_length','29') ON CONFLICT (key) DO UPDATE SET value='29'"))
    # Identifier is generated only from a constant prefix and uuid4.hex.
    db.execute(text(f"INSERT INTO {shadow}.platform_settings (key,value) VALUES "  # noqa: S608
                    "('bootstrap_password_min_length','3')"))
    connection = db.connection()
    previous = connection.get_execution_options().get('schema_translate_map')
    try:
        connection.execution_options(schema_translate_map={None if translation == 'default' else 'public': shadow})
        target = db if surface == 'session' else connection
        assert get_all(target)['bootstrap_password_min_length'] == '29'
        assert effective(target, require_store=True).bootstrap_password_min_length == 29
    finally:
        connection.execution_options(schema_translate_map=previous)
