"""Kernel setting-spec declarations for Academy operator settings (ADR-0003).

Phase 0 of the settings-resolver adoption: declare a kernel
`SettingSpec` for every key the legacy `app.settings_store` serves, over
the same `domain_settings` rows (domain ``academy``, platform scope).
Declaring and registering specs changes nothing that is served — the
legacy store remains authoritative until the shadow sweep
(`python -m app.cli settings-shadow-sweep`) reports zero divergence on
real data and the deployment phase is advanced deliberately.

Type mapping from the legacy defaults: bool/int/str use the kernel
types, tuple role lists use ``list`` (JSON storage), dicts use ``json``,
and floats use the Academy-declared ``float`` value type below (the
kernel ships no float type). Legacy per-key validators ride along as
spec validators so kernel-side validation rejects what the legacy
store rejects; normalisation differences are exactly what the shadow
sweep exists to surface.
"""

from __future__ import annotations

import copy
import math
from typing import Any

from dotmac_kernel.setting_value_types import SettingValueType, ValueTypeSpec
from dotmac_kernel.settings_models import SettingDomain
from dotmac_kernel.settings_resolver import SettingSpec, register_specs

from app import settings_store

ACADEMY_DOMAIN = SettingDomain("academy")


def _float_from(raw: object) -> object | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
    elif isinstance(raw, str):
        try:
            value = float(raw.strip())
        except ValueError:
            return None
    else:
        return None
    return value if math.isfinite(value) else None


def _float_to(value: object) -> object:
    coerced = _float_from(value)
    if coerced is None:
        raise ValueError(f"not a finite float: {value!r}")
    return repr(coerced)


FLOAT_VALUE_TYPE = ValueTypeSpec(
    code=SettingValueType("float"),
    storage="text",
    from_storage=_float_from,
    to_storage=_float_to,
    description="A finite floating-point number.",
)

_TYPE_BY_PYTYPE: dict[type, SettingValueType] = {
    bool: SettingValueType("boolean"),
    int: SettingValueType("integer"),
    float: SettingValueType("float"),
    str: SettingValueType("string"),
    tuple: SettingValueType("list"),
    list: SettingValueType("list"),
    dict: SettingValueType("json"),
}


def _validator_for(key: str):
    def _validate(value: object) -> None:
        # Legacy validators return the normalised value; the kernel spec
        # validator only adjudicates. A raise means invalid, as legacy.
        settings_store._validated(key, value)

    _validate.__qualname__ = f"academy_setting_validator[{key}]"
    return _validate


def _build_specs() -> tuple[SettingSpec[Any], ...]:
    defaults = settings_store._defaults()
    specs: list[SettingSpec[Any]] = []
    for key in settings_store._POLICY_KEYS:
        default = defaults[key]
        value_type = _TYPE_BY_PYTYPE[type(default)]
        if isinstance(default, tuple):
            default = list(default)
        elif isinstance(default, (dict, list)):
            default = copy.deepcopy(default)
        specs.append(
            SettingSpec(
                domain=ACADEMY_DOMAIN,
                key=key,
                value_type=value_type,
                default=default,
                validator=_validator_for(key),
            )
        )
    return tuple(specs)


ACADEMY_SETTING_SPECS: tuple[SettingSpec[Any], ...] = _build_specs()


def register_academy_setting_specs() -> None:
    """Register Academy specs with the kernel resolver.

    Deliberately not done at import: registration validates each spec's
    default against the *installed* value-type registry, and the float
    type is installed from the Academy manifest by `create_app` (or by
    `install_academy_setting_registries` in CLI/test processes). Import
    order must never decide whether boot succeeds.
    """
    register_specs(list(ACADEMY_SETTING_SPECS))


def install_academy_setting_registries() -> None:
    """Install domain/value-type registries outside `create_app` (CLI sweeps)."""
    from dotmac_kernel.setting_domains import SettingDomainRegistry, install_setting_domains
    from dotmac_kernel.setting_value_types import SettingValueTypeRegistry, install_setting_value_types

    from app.owner_composition import compose_migration_owner_declarations

    manifests = compose_migration_owner_declarations()
    install_setting_domains(SettingDomainRegistry.from_manifests(manifests))
    install_setting_value_types(SettingValueTypeRegistry.from_manifests(manifests))
    register_academy_setting_specs()
