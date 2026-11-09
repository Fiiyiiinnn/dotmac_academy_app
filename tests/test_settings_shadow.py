"""ADR-0003 phase-0 settings adoption: spec coverage + shadow sweep gate.

The legacy `app.settings_store` stays authoritative; these tests prove
the kernel resolver can see every Academy key through declared specs
and agrees with the legacy resolution over stored rows of each type.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app import settings_store
from app.settings_specs import (
    ACADEMY_SETTING_SPECS,
    FLOAT_VALUE_TYPE,
    _float_from,
    _float_to,
    install_academy_setting_registries,
)


def test_specs_cover_every_known_key_exactly_once():
    keys = [spec.key for spec in ACADEMY_SETTING_SPECS]
    assert sorted(keys) == sorted(settings_store._POLICY_KEYS)
    assert len(keys) == len(set(keys))
    assert all(str(spec.domain) == "academy" for spec in ACADEMY_SETTING_SPECS)


def test_float_value_type_round_trip_and_refusals():
    assert _float_from("1.5") == 1.5
    assert _float_from(2) == 2.0
    assert _float_from(True) is None
    assert _float_from("nan") is None
    assert _float_from("junk") is None
    assert _float_from(_float_to(0.25)) == 0.25
    with pytest.raises(ValueError, match="not a finite float"):
        _float_to(float("inf"))
    float_keys = [k for k, v in settings_store._DEFAULTS.items() if isinstance(v, float)]
    by_key = {spec.key: spec for spec in ACADEMY_SETTING_SPECS}
    assert float_keys and all(by_key[k].value_type == FLOAT_VALUE_TYPE.code for k in float_keys)


_SUBSET = (
    "smtp_starttls",
    "smtp_port",
    "smtp_host",
    "learning_lab_pass_threshold",
    "admin_home_access_roles",
)


def _legacy_reader_for(db):
    legacy = settings_store.effective(db, require_store=True)

    def _read(domain, key):
        value = legacy[key]
        return list(value) if isinstance(value, tuple) else value

    return _read


def test_shadow_sweep_clean_over_stored_rows(genesis_database):
    from dotmac_kernel.settings_shadow import sweep

    admin, _superuser, _name = genesis_database
    install_academy_setting_registries()
    specs = [s for s in ACADEMY_SETTING_SPECS if s.key in _SUBSET]
    assert len(specs) == len(_SUBSET)
    with Session(admin) as db:
        settings_store.set_many(
            db,
            {
                "smtp_starttls": False,
                "smtp_port": 2525,
                "smtp_host": "mail.example",
                "learning_lab_pass_threshold": 0.5,
                "admin_home_access_roles": ("admin", "instructor"),
            },
        )
        db.commit()
    with Session(admin) as db:
        report = sweep(db, _legacy_reader_for(db), specs=specs)
    assert report.clean, report.describe()
    assert report.compared == len(_SUBSET)


def test_shadow_sweep_reports_divergence_without_values(genesis_database):
    from dotmac_kernel.settings_shadow import sweep

    admin, _superuser, _name = genesis_database
    install_academy_setting_registries()
    specs = [s for s in ACADEMY_SETTING_SPECS if s.key == "smtp_port"]
    with Session(admin) as db:
        report = sweep(db, lambda domain, key: 9999, specs=specs)
    assert not report.clean
    assert report.divergences[0].key == "smtp_port"
    assert "9999" not in report.describe()
