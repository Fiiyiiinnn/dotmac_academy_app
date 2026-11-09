"""Hosted PostgreSQL qualification for the kernel-backed settings store.

Operator values live in the kernel's `domain_settings` (domain
`academy`, platform scope); `effective()` merges them over product
defaults, `set_many` refuses unknown keys, and strict consumers
(`require_store=True`, used by learning policy composition) never
authorize from fallback when the store session is absent.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.services import learning_policy, settings_store


def test_stored_value_overrides_default_in_learning_terms(genesis_database):
    admin, _superuser, _name = genesis_database
    with Session(admin) as db, db.begin():
        settings_store.set_many(db, {"learning_todo_graded_limit": 3})
    with Session(admin) as db:
        terms = learning_policy.todo_terms(db)
        assert terms.graded_limit == 3
        assert terms.graded_lookback_days == 14


def test_set_many_refuses_unknown_keys(genesis_database):
    admin, _superuser, _name = genesis_database
    with Session(admin) as db, db.begin():
        with pytest.raises(ValueError, match="unknown academy setting keys"):
            settings_store.set_many(db, {"definitely_not_a_key": 1})


def test_require_store_without_session_refuses() -> None:
    with pytest.raises(ValueError, match="settings database session is required"):
        settings_store.effective(None, require_store=True)


def test_kernel_storage_refuses_null_rows_and_malformed_text_refuses(genesis_database):
    """Kernel domain_settings forbids NULL values; malformed text refuses.

    The legacy platform_settings table allowed stored NULL policy rows
    (which its store then refused at read). Kernel storage prevents the
    row outright (ck_domain_settings_value_alignment); a malformed
    stored text value still refuses through the strict validator, and
    valid overrides round-trip through set_many/delete.
    """
    from dotmac_kernel.settings_models import DomainSetting
    from sqlalchemy.exc import IntegrityError

    admin, _superuser, _name = genesis_database
    with Session(admin) as db:
        db.add(
            DomainSetting(
                tenant_id=None,
                scope_kind="platform",
                domain="academy",
                key="admission_enrollment_initial_status",
                value_text=None,
                value_json=None,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
    with Session(admin) as db, db.begin():
        db.add(
            DomainSetting(
                tenant_id=None,
                scope_kind="platform",
                domain="academy",
                key="admission_enrollment_initial_status",
                value_text="x" * 21,
                value_json=None,
            )
        )
    with Session(admin) as db:
        with pytest.raises(ValueError):
            settings_store.effective(db, require_store=True)
    with Session(admin) as db, db.begin():
        settings_store.set_many(db, {"admission_enrollment_initial_status": "dropped"})
    with Session(admin) as db:
        assert settings_store.effective(db, require_store=True).admission_enrollment_initial_status == "dropped"
    with Session(admin) as db, db.begin():
        settings_store.delete(db, "admission_enrollment_initial_status")
    with Session(admin) as db:
        assert settings_store.effective(db, require_store=True).admission_enrollment_initial_status == "active"
