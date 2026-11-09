"""Tests for the platform settings store: effective() merge + type coercion."""

from __future__ import annotations

import pytest

from app.config import settings
from app.models.platform_settings import PlatformSetting
from app.services import settings_store
from app.services.settings_store import effective, get_all, set_many


def _clear(db):
    db.query(PlatformSetting).delete()
    db.flush()


def test_effective_falls_back_to_env_defaults(admin_session, monkeypatch):
    """With no stored rows, effective() returns the env/config defaults."""
    _clear(admin_session)
    monkeypatch.setattr(settings, "smtp_host", "env-host.example", raising=False)
    monkeypatch.setattr(settings, "max_concurrent_labs", 7, raising=False)
    monkeypatch.setattr(settings, "max_concurrent_labs_per_tenant", 0, raising=False)

    cfg = effective(admin_session)
    assert cfg.smtp_host == "env-host.example"
    assert cfg.max_concurrent_labs == 7
    assert cfg.max_concurrent_labs_per_tenant == 0
    # Literal defaults for keys with no env source.
    assert cfg.branding_name == "Dotmac Academy"
    assert cfg.email_auto_on_pass is True
    assert cfg.email_digest_enabled is True
    admin_session.rollback()


def test_effective_db_overrides_with_type_coercion(admin_session):
    """set_many then effective() reflects overrides, coerced to native types."""
    _clear(admin_session)
    set_many(
        admin_session,
        {
            "smtp_host": "smtp.store.example",
            "smtp_port": "2525",
            "smtp_starttls": "false",
            "email_auto_on_pass": "false",
            "branding_name": "Acme Academy",
            "max_concurrent_labs": "3",
            "max_concurrent_labs_per_tenant": "2",
            "lab_idle_minutes": "15",
        },
    )

    cfg = effective(admin_session)
    assert cfg.smtp_host == "smtp.store.example"
    assert cfg.smtp_port == 2525 and isinstance(cfg.smtp_port, int)
    assert cfg.smtp_starttls is False
    assert cfg.email_auto_on_pass is False
    assert cfg.branding_name == "Acme Academy"
    assert cfg.max_concurrent_labs == 3
    assert cfg.max_concurrent_labs_per_tenant == 2
    assert cfg.lab_idle_minutes == 15

    # get_all returns the raw stored strings.
    raw = get_all(admin_session)
    assert raw["smtp_port"] == "2525"
    admin_session.rollback()


def test_effective_safe_when_db_none():
    """effective(None) never touches the DB and returns env defaults."""
    cfg = effective(None)
    assert cfg.smtp_port == settings.smtp_port
    assert cfg.branding_name == "Dotmac Academy"


def test_set_many_upserts(admin_session):
    """set_many updates an existing key instead of duplicating it."""
    _clear(admin_session)
    set_many(admin_session, {"branding_name": "First"})
    set_many(admin_session, {"branding_name": "Second"})
    assert get_all(admin_session)["branding_name"] == "Second"
    assert effective(admin_session).branding_name == "Second"
    admin_session.rollback()


def test_lab_limits_reject_invalid_writes_and_refuse_stored_invalid_rows(
    admin_session,
):
    _clear(admin_session)
    with pytest.raises(ValueError, match="max_concurrent_labs_per_tenant must be an integer between 0 and 2147483647"):
        set_many(admin_session, {"max_concurrent_labs_per_tenant": "-1"})

    admin_session.add(
        PlatformSetting(key="max_concurrent_labs_per_tenant", value="-1")
    )
    admin_session.flush()
    # Strict policy read: a stored invalid lab limit is refused like every other policy key.
    with pytest.raises(ValueError, match="max_concurrent_labs_per_tenant must be an integer between 0 and 2147483647"):
        effective(admin_session)
    admin_session.rollback()


def test_effective_falls_back_when_table_query_fails(admin_session, monkeypatch):
    """A failing get_all is swallowed; env defaults are returned."""
    def boom(_db):
        raise RuntimeError("table gone")

    monkeypatch.setattr(settings_store, "get_all", boom)
    cfg = effective(admin_session)
    assert cfg.branding_name == "Dotmac Academy"
    admin_session.rollback()


def test_new_operational_keys_are_registered(admin_session):
    """Regression: `effective` merges a stored value only when the key exists in
    the defaults registry. `learner_digest_enabled` and
    `reminder_inactivity_max_nudges` were read by their services but never
    registered, so storing them did nothing — the digest switch would have been
    flipped and silently kept sending nothing."""
    from app.services import settings_store

    assert "learner_digest_enabled" in settings_store.KNOWN_KEYS
    assert "reminder_inactivity_max_nudges" in settings_store.KNOWN_KEYS

    # Off by default: enabling it mails every active learner.
    assert settings_store.effective(admin_session).get("learner_digest_enabled") is False

    settings_store.set_many(admin_session, {"learner_digest_enabled": "true"})
    settings_store.set_many(admin_session, {"reminder_inactivity_max_nudges": "6"})
    eff = settings_store.effective(admin_session)
    assert eff.get("learner_digest_enabled") is True  # coerced to bool, not "true"
    assert eff.get("reminder_inactivity_max_nudges") == 6  # coerced to int
    admin_session.rollback()
