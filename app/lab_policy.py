"""Lab execution policy: shapes, validators, immutable policy value.

Ported verbatim from the D8 Academy config at new-composition genesis;
`LabExecutionPolicy.from_settings` reads the product Settings fields.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, cast, overload

LAB_EXECUTION_POLICY_SHAPES = {
    "lab_command_timeout_seconds_by_action": ("inspect", "deploy", "destroy", "check"),
    "lab_operation_lease_seconds_by_kind": ("deploy", "destroy", "check"),
    "lab_operation_max_attempts_by_kind": ("deploy", "destroy", "check"),
    "lab_operation_retry_delay_seconds": (),
    "lab_worker_poll_interval_seconds": (),
    "lab_restart_reclaim_lock_timeout_seconds": (),
    "lab_operation_unknown_lease_seconds": (),
    "lab_operation_unknown_max_attempts": (),
    "lab_process_termination_grace_seconds": (),
}
LAB_CAPACITY_KEYS = ("max_concurrent_labs", "max_concurrent_labs_per_tenant", "lab_idle_minutes")
LAB_EXECUTION_OVERRIDE_KEY = "lab_execution_policy_overrides"


def _lab_json(value: object) -> object:
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError("duplicate lab policy key")
            result[key] = item
        return result

    if isinstance(value, str):
        try:
            return json.loads(value, object_pairs_hook=pairs)
        except (ValueError, TypeError):
            raise ValueError("invalid lab policy JSON") from None
    return value


def validate_lab_policy_integer(key: str, value: object, *, text: bool = True) -> int:
    if text and isinstance(value, str) and value.isascii() and value.isdecimal():
        value = int(value)
    minimum = 0 if key == "max_concurrent_labs_per_tenant" else 1
    if type(value) is not int or value < minimum or value > 2147483647:
        raise ValueError(f"{key} must be an integer between {minimum} and 2147483647")
    return value


@overload
def validate_lab_execution_value(
    key: Literal[
        "lab_command_timeout_seconds_by_action",
        "lab_operation_lease_seconds_by_kind",
        "lab_operation_max_attempts_by_kind",
    ],
    value: object,
) -> dict[str, int]: ...


@overload
def validate_lab_execution_value(key: str, value: object) -> object: ...


def validate_lab_execution_value(key: str, value: object) -> object:
    shape = LAB_EXECUTION_POLICY_SHAPES[key]
    if not shape:
        return validate_lab_policy_integer(key, value)
    value = _lab_json(value)
    if type(value) is not dict or set(value) != set(shape):
        raise ValueError(f"{key} must contain the exact action/kind keys")
    return {name: validate_lab_policy_integer(key, value[name], text=False) for name in shape}


def validate_lab_execution_overrides(value: object) -> dict[str, object]:
    value = _lab_json(value)
    if type(value) is not dict or set(value) - LAB_EXECUTION_POLICY_SHAPES.keys():
        raise ValueError("lab execution overrides must be an object of known fields")
    # JSON members use numbers, not numeric strings. Absent members inherit env.
    for key, item in value.items():
        if not LAB_EXECUTION_POLICY_SHAPES[key]:
            validate_lab_policy_integer(key, item, text=False)
    return {key: validate_lab_execution_value(key, item) for key, item in value.items()}


def validate_lab_execution_relations(values: Mapping[str, object]) -> None:
    waits = validate_lab_execution_value(
        "lab_command_timeout_seconds_by_action", values["lab_command_timeout_seconds_by_action"]
    )
    leases = validate_lab_execution_value(
        "lab_operation_lease_seconds_by_kind", values["lab_operation_lease_seconds_by_kind"]
    )
    grace = validate_lab_policy_integer(
        "lab_process_termination_grace_seconds", values["lab_process_termination_grace_seconds"]
    )
    inspect, deploy, destroy, check = (waits[key] for key in ("inspect", "deploy", "destroy", "check"))
    bounds = {
        "deploy": max(inspect + destroy, inspect + deploy, 2 * inspect) + 2 * grace,
        "destroy": 2 * inspect + destroy + 2 * grace,
        "check": check + 2 * grace,
    }
    if any(leases[kind] <= bound for kind, bound in bounds.items()):
        raise ValueError("lab lease must exceed configured command and cleanup waits")
    if cast(int, values["lab_operation_unknown_lease_seconds"]) <= inspect + 2 * grace:
        raise ValueError("unknown lab lease must exceed inspect and cleanup waits")


@dataclass(frozen=True)
class LabExecutionPolicy:
    """Immutable policy for one startup phase or worker pass.

    Lease relations bound configured command/cleanup waits, not total wall time.
    """

    lab_command_timeout_seconds_by_action: Mapping[str, int]
    lab_operation_lease_seconds_by_kind: Mapping[str, int]
    lab_operation_max_attempts_by_kind: Mapping[str, int]
    lab_operation_retry_delay_seconds: int
    lab_worker_poll_interval_seconds: int
    lab_restart_reclaim_lock_timeout_seconds: int
    lab_operation_unknown_lease_seconds: int
    lab_operation_unknown_max_attempts: int
    lab_process_termination_grace_seconds: int
    max_concurrent_labs: int
    max_concurrent_labs_per_tenant: int
    lab_idle_minutes: int

    def __post_init__(self):
        values = {}
        for key, shape in LAB_EXECUTION_POLICY_SHAPES.items():
            raw = getattr(self, key)
            # Copy immutable projections too, so callers cannot retain a backing dict.
            values[key] = validate_lab_execution_value(key, dict(raw) if shape and isinstance(raw, Mapping) else raw)
        for key in LAB_CAPACITY_KEYS:
            values[key] = validate_lab_policy_integer(key, getattr(self, key))
        validate_lab_execution_relations(values)
        for key, value in values.items():
            object.__setattr__(self, key, MappingProxyType(value) if isinstance(value, dict) else value)

    @classmethod
    def from_settings(cls, value):
        return cls(**{key: getattr(value, key) for key in (*LAB_EXECUTION_POLICY_SHAPES, *LAB_CAPACITY_KEYS)})

    def lease_seconds(self, kind: str) -> int:
        return self.lab_operation_lease_seconds_by_kind.get(kind, self.lab_operation_unknown_lease_seconds)

    def max_attempts(self, kind: str) -> int:
        return self.lab_operation_max_attempts_by_kind.get(kind, self.lab_operation_unknown_max_attempts)


def validate_lab_check_output_limit(value: object) -> int:
    """Strict positive output length; empty/bool/float values never fall back."""
    message = "lab check output limit must be a positive integer"
    if isinstance(value, str) and value.strip().isascii() and value.strip().isdecimal():
        try:
            value = int(value.strip())
        except ValueError:
            raise ValueError(message) from None
    if type(value) is not int or value < 1:
        raise ValueError(message)
    return value
