"""Academy operator settings over the kernel settings store.

The legacy app kept these in its own `platform_settings` table. In this
composition the kernel's `domain_settings` (domain `academy`, platform
scope) is the store; env/product defaults below are the fallback. Only
the keys the ported slices consume are known here — the registry grows
with each port, exactly like the slices do. Unknown stored keys are
ignored on read and refused on write (`set_many`).

`effective(require_store=True)` keeps the legacy strict posture: a
missing session or a failed read propagates instead of authorizing
from fallback values.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

DOMAIN = "academy"

# Canonical settings always address public.domain_settings with literal
# schema-qualified SQL: caller search_path and schema_translate_map can
# shadow the kernel model's unqualified table, but never a text literal.
_DS_SELECT = (
    "SELECT key, value_text, value_json FROM public.domain_settings "
    "WHERE domain = :domain AND scope_kind = :scope_kind AND tenant_id IS NULL"
)


def _flush_if_autoflush(db: Session | Connection) -> None:
    """Text statements skip ORM autoflush; preserve pending semantics."""
    if isinstance(db, Session) and db.autoflush:
        db.flush()


class LabExecutionPolicyConflict(RuntimeError):
    """A concurrent whole-object lab-policy write won; this one lost.

    Raised before any write of the losing call, with the caller's
    transaction left alive (no hidden abort, commit, or retry).
    """


from app import admissions_settings as _v  # noqa: E402
from app.config import settings  # noqa: E402  (legacy live-defaults seam)
from app.lab_policy import (  # noqa: E402
    LAB_CAPACITY_KEYS as _LAB_CAPACITY_KEYS,
)
from app.lab_policy import (  # noqa: E402
    LAB_EXECUTION_OVERRIDE_KEY as _LAB_OVERRIDE_KEY,
)
from app.lab_policy import (  # noqa: E402
    LAB_EXECUTION_POLICY_SHAPES as _LAB_SHAPES,
)

_POLICY_KEYS: tuple[str, ...] = tuple(
    sorted(
        {
            *_v.BOOTSTRAP_POLICY_KEYS,
            *_v.ENTRANCE_INVITATION_POLICY_KEYS,
            *_v.ADMISSION_ENROLLMENT_POLICY_KEYS,
            *_v.ENROLLMENT_INITIAL_STATE_POLICY_KEYS,
            *_v.INITIAL_IDENTITY_POLICY_KEYS,
            *_v.ONBOARDING_POLICY_KEYS,
            *_v.AUTHENTICATION_POLICY_SHAPES,
            *_v.INVITATION_PURPOSE_POLICY_SHAPES,
            *_v.PROFILE_POLICY_SHAPES,
            *_v.PASSWORD_CHANGE_POLICY_SHAPES,
            *_v.LEARNING_POLICY_SHAPES,
            *_v.ICAL_FEED_POLICY_KEYS,
            *_LAB_CAPACITY_KEYS,
            *_LAB_SHAPES,
            _LAB_OVERRIDE_KEY,
            "admin_home_access_roles",
            "bank_author_roles",
            "bank_export_roles",
            "bank_publish_roles",
            "bank_repoint_roles",
            "bank_retire_roles",
            "branding_name",
            "catalog_staff_roles",
            "email_auto_on_pass",
            "email_digest_enabled",
            "entrance_questions_per_category",
            "gradebook_access_roles",
            "invitation_default_member_role",
            "invitation_member_role_map",
            "lab_admin_access_roles",
            "learner_digest_enabled",
            "management_inquiry_recipient",
            "notifications_recent_limit",
            "password_reset_min_length",
            "password_reset_token_minutes",
            "reminder_digest_hour",
            "reminder_digest_weekday",
            "reminder_inactivity_days",
            "reminder_inactivity_max_nudges",
            "reminders_admin_access_roles",
            "reminders_enabled",
            "reports_access_roles",
            "settings_access_roles",
            "smtp_from",
            "smtp_host",
            "smtp_port",
            "smtp_starttls",
            "smtp_user",
            "success_certificate_grace_hours",
            "success_inactive_days",
            "success_min_grade_pct",
            "teaching_access_roles",
            "web_account_creation_password_min_length",
            "web_account_creation_role_admission",
            "web_account_creation_role_provisioning",
        }
    )
)


def _defaults() -> dict[str, Any]:
    """Env/config defaults for every known key (read live so tests can patch).

    Mirrors the legacy store: most policy families are validated here;
    entrance/admission/enrollment families are validated post-merge in
    `effective()` so a stored override is never masked by an invalid
    unused env default.
    """
    from app import admissions_settings as v
    from app.lab_policy import (
        LAB_EXECUTION_POLICY_SHAPES,
        validate_lab_execution_value,
    )

    merged: dict[str, Any] = {key: getattr(settings, key) for key in _POLICY_KEYS}
    validated_keyed = (
        (v.BOOTSTRAP_POLICY_KEYS, v.validate_bootstrap_policy_value),
        (v.ONBOARDING_POLICY_KEYS, v.validate_onboarding_policy_value),
        (v.INITIAL_IDENTITY_POLICY_KEYS, v.validate_initial_identity_policy_value),
        (v.INVITATION_PURPOSE_POLICY_SHAPES, v.validate_invitation_purpose_policy_value),
        (v.PASSWORD_CHANGE_POLICY_SHAPES, v.validate_password_change_policy_value),
        (v.PROFILE_POLICY_SHAPES, v.validate_profile_policy_value),
        (v.AUTHENTICATION_POLICY_SHAPES, v.validate_authentication_policy_value),
        (v.LEARNING_POLICY_SHAPES, v.validate_learning_policy_value),
        (v.ICAL_FEED_POLICY_KEYS, v.validate_ical_feed_policy),
    )
    for keys, fn in validated_keyed:
        for key in keys:
            merged[key] = fn(key, merged[key])
    for key in LAB_EXECUTION_POLICY_SHAPES:
        merged[key] = validate_lab_execution_value(key, merged[key])
    singles = {
        "admin_home_access_roles": v.validate_admin_home_access_roles,
        "catalog_staff_roles": v.validate_catalog_staff_roles,
        "gradebook_access_roles": v.validate_gradebook_access_roles,
        "invitation_default_member_role": v.validate_invitation_default_member_role,
        "invitation_member_role_map": v.validate_invitation_member_role_map,
        "lab_admin_access_roles": v.validate_lab_admin_access_roles,
        "notifications_recent_limit": v.validate_notifications_recent_limit,
        "password_reset_min_length": v.validate_password_reset_min_length,
        "reminders_admin_access_roles": v.validate_reminders_admin_access_roles,
        "reports_access_roles": v.validate_reports_access_roles,
        "settings_access_roles": v.validate_settings_access_roles,
        "teaching_access_roles": v.validate_teaching_access_roles,
        "web_account_creation_password_min_length": v.validate_web_account_creation_password_min_length,
        "web_account_creation_role_admission": v.validate_web_account_creation_role_admission,
        "web_account_creation_role_provisioning": v.validate_web_account_creation_role_provisioning,
    }
    for key, fn in singles.items():
        merged[key] = fn(merged[key])
    for key, fn in _bank_role_validators().items():
        merged[key] = fn(merged[key])
    # Env-only secret: readable here, never a stored setting (set_many
    # refuses it as an unknown key).
    merged["smtp_password"] = settings.smtp_password
    return merged


# Import-time snapshot for key membership only; values are read live.
_DEFAULTS: dict[str, Any] = {key: getattr(settings, key) for key in _POLICY_KEYS}

KNOWN_KEYS: tuple[str, ...] = tuple(sorted(_DEFAULTS))


class EffectiveSettings:
    """Attribute-access view of the merged (DB-over-env) known settings.

    Attributes are populated dynamically from the known keys (the legacy
    shape); ``cfg["key"]`` item access is kept as a convenience.
    """

    def __init__(self, values: dict[str, Any]) -> None:
        self.__dict__.update(values)

    def __getitem__(self, key: str) -> Any:  # dict-like convenience
        return self.__dict__[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.__dict__.get(key, default)

    def __contains__(self, key: object) -> bool:
        return key in self.__dict__

    def keys(self):  # type: ignore[no-untyped-def]
        return self.__dict__.keys()

    def items(self):  # type: ignore[no-untyped-def]
        return self.__dict__.items()


def _validated(key: str, value: Any) -> Any:
    """Run the strict per-key validator when one owns this key (D8 parity).

    Returns ``(True, validated)`` when a validator applied, else
    ``(False, value)`` so the generic coercion below handles plain keys.
    Malformed stored policy values raise — they never fall back.
    """
    from app import admissions_settings as v
    from app.lab_policy import LAB_CAPACITY_KEYS, LAB_EXECUTION_OVERRIDE_KEY

    if key == LAB_EXECUTION_OVERRIDE_KEY:
        from app.lab_policy import validate_lab_execution_overrides

        return True, validate_lab_execution_overrides(value)
    if key in LAB_CAPACITY_KEYS:
        from app.lab_policy import validate_lab_policy_integer

        return True, validate_lab_policy_integer(key, value)
    from app.lab_policy import LAB_EXECUTION_POLICY_SHAPES

    if key in LAB_EXECUTION_POLICY_SHAPES:
        from app.lab_policy import validate_lab_execution_value

        return True, validate_lab_execution_value(key, value)
    bank = _bank_role_validators().get(key)
    if bank is not None:
        return True, bank(value)
    singles = {
        "admin_home_access_roles": v.validate_admin_home_access_roles,
        "catalog_staff_roles": v.validate_catalog_staff_roles,
        "gradebook_access_roles": v.validate_gradebook_access_roles,
        "invitation_default_member_role": v.validate_invitation_default_member_role,
        "invitation_member_role_map": v.validate_invitation_member_role_map,
        "lab_admin_access_roles": v.validate_lab_admin_access_roles,
        "notifications_recent_limit": v.validate_notifications_recent_limit,
        "password_reset_min_length": v.validate_password_reset_min_length,
        "reminders_admin_access_roles": v.validate_reminders_admin_access_roles,
        "reports_access_roles": v.validate_reports_access_roles,
        "settings_access_roles": v.validate_settings_access_roles,
        "teaching_access_roles": v.validate_teaching_access_roles,
        "web_account_creation_password_min_length": v.validate_web_account_creation_password_min_length,
        "web_account_creation_role_admission": v.validate_web_account_creation_role_admission,
        "web_account_creation_role_provisioning": v.validate_web_account_creation_role_provisioning,
    }
    if key in singles:
        return True, singles[key](value)
    if key in v.ENTRANCE_INVITATION_POLICY_KEYS:
        return True, v.validate_entrance_invitation_deadline_days(value)
    keyed = (
        (v.BOOTSTRAP_POLICY_KEYS, v.validate_bootstrap_policy_value),
        (v.ENROLLMENT_INITIAL_STATE_POLICY_KEYS, v.validate_enrollment_initial_state_policy_value),
        (v.ADMISSION_ENROLLMENT_POLICY_KEYS, v.validate_admission_enrollment_policy_value),
        (v.ONBOARDING_POLICY_KEYS, v.validate_onboarding_policy_value),
        (v.INITIAL_IDENTITY_POLICY_KEYS, v.validate_initial_identity_policy_value),
        (v.INVITATION_PURPOSE_POLICY_SHAPES, v.validate_invitation_purpose_policy_value),
        (v.PASSWORD_CHANGE_POLICY_SHAPES, v.validate_password_change_policy_value),
        (v.PROFILE_POLICY_SHAPES, v.validate_profile_policy_value),
        (v.AUTHENTICATION_POLICY_SHAPES, v.validate_authentication_policy_value),
        (v.LEARNING_POLICY_SHAPES, v.validate_learning_policy_value),
        (v.ICAL_FEED_POLICY_KEYS, v.validate_ical_feed_policy),
    )
    for keys, fn in keyed:
        if key in keys:
            return True, fn(key, value)
    return False, value


_BANK_ROLE_VALIDATORS: dict[str, Any] = {}


def _bank_role_validators() -> dict[str, Any]:
    if not _BANK_ROLE_VALIDATORS:
        from app import admissions_settings as v

        _BANK_ROLE_VALIDATORS.update(
            {
                "bank_author_roles": v.validate_bank_author_roles,
                "bank_export_roles": v.validate_bank_export_roles,
                "bank_publish_roles": v.validate_bank_publish_roles,
                "bank_repoint_roles": v.validate_bank_repoint_roles,
                "bank_retire_roles": v.validate_bank_retire_roles,
            }
        )
    return _BANK_ROLE_VALIDATORS


def _expand_lab_overrides(merged: dict[str, Any], stored_keys: Iterable[str] = ()) -> None:
    """Apply the whole-object lab overrides onto the split policy keys.

    The overrides object is the only stored lab-policy surface: a split
    lab key stored as its own row is refused rather than silently
    becoming a default. Absent override members inherit env values.
    """
    from app.lab_policy import LAB_EXECUTION_POLICY_SHAPES, validate_lab_execution_overrides

    split = sorted(set(stored_keys) & set(LAB_EXECUTION_POLICY_SHAPES))
    if split:
        raise ValueError(
            f"split lab execution policy rows are refused ({', '.join(split)}); use lab_execution_policy_overrides"
        )
    overrides = merged.get(_LAB_OVERRIDE_KEY)
    if overrides is None:
        return
    merged.update(validate_lab_execution_overrides(overrides))


def _validate_relations(merged: dict[str, Any]) -> None:
    from app import admissions_settings as v
    from app.lab_policy import validate_lab_execution_relations

    v.validate_learning_policy_relations(merged)
    validate_lab_execution_relations(merged)


def _coerce(key: str, value: Any, default: Any = None) -> Any:
    """Coerce a stored JSON/text value to the default's declared type."""
    applied, validated = _validated(key, value)
    if applied:
        return validated
    if value is None:
        return _DEFAULTS[key] if default is None else default
    default = _DEFAULTS[key]
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    if isinstance(default, int):
        return int(value)
    if isinstance(default, float):
        return float(value)
    if isinstance(default, str):
        return str(value)
    if isinstance(default, (dict, list)):
        if isinstance(value, str):
            value = json.loads(value)
        return type(default)(value)
    return value


def effective(db: Session | Connection | None = None, *, require_store: bool = False) -> EffectiveSettings:
    """Return known settings with DB values overriding env defaults.

    Cheap + safe: if ``db`` is None, or the read fails for any reason,
    the env/config defaults are returned unchanged. Security policy
    composition uses ``require_store=True`` to refuse missing sessions
    and propagate read failures rather than authorize from fallback.
    Malformed policy values always refuse instead of falling back.
    """
    if require_store and db is None:
        raise ValueError("a settings database session is required")
    merged = _defaults()
    if db is not None:
        try:
            stored = get_all(db)
        except Exception:
            if require_store:
                raise
            stored = {}
        for key, raw in stored.items():
            if key in merged:
                merged[key] = _coerce(key, raw, merged[key])
        _expand_lab_overrides(merged, stored.keys())
    else:
        _expand_lab_overrides(merged)
    from app import admissions_settings as v

    for key in v.ENTRANCE_INVITATION_POLICY_KEYS:
        merged[key] = v.validate_entrance_invitation_deadline_days(merged[key])
    for key in v.ENROLLMENT_INITIAL_STATE_POLICY_KEYS:
        merged[key] = v.validate_enrollment_initial_state_policy_value(key, merged[key])
    for key in v.ADMISSION_ENROLLMENT_POLICY_KEYS:
        merged[key] = v.validate_admission_enrollment_policy_value(key, merged[key])
    _validate_relations(merged)
    return EffectiveSettings(merged)


def execution_policy(db: Session):
    """Read exactly one strict store snapshot through the actual worker Session."""
    from app.lab_policy import LabExecutionPolicy

    return LabExecutionPolicy.from_settings(effective(db, require_store=True))


_ABSENT = object()


def _lab_proposal(db: Session, updates: Mapping[str, Any], *, removed: str | None = None) -> Any:
    """Snapshot the stored lab-overrides value a write is based on.

    Reads without locking: two concurrent whole-object writers observe
    the same base here, and `_lab_cas_guard` then lets exactly one win.
    """

    stored = get_all(db)
    return stored.get(_LAB_OVERRIDE_KEY, _ABSENT)


def _lab_cas_guard(db: Session, proposal: Any) -> None:
    """Refuse a lab-policy write whose base snapshot lost a race.

    Serializes writers on a transaction-scoped advisory lock, then
    re-reads the stored value on a fresh snapshot; any change since the
    proposal (write or delete by either side) raises
    `LabExecutionPolicyConflict` before this call writes anything.
    """

    db.execute(text("SELECT pg_advisory_xact_lock(hashtext('academy:lab_execution_policy_overrides'))"))
    db.expire_all()
    current = get_all(db).get(_LAB_OVERRIDE_KEY, _ABSENT)
    if current is _ABSENT or proposal is _ABSENT:
        if current is not proposal:
            raise LabExecutionPolicyConflict("lab execution policy changed concurrently")
        return
    if json.dumps(current, sort_keys=True, default=str) != json.dumps(proposal, sort_keys=True, default=str):
        raise LabExecutionPolicyConflict("lab execution policy changed concurrently")


def set_many(db: Session, values: Mapping[str, Any]) -> None:
    """Upsert platform-scope operator settings; unknown keys are refused.

    Every value is validated (and relations checked) before the
    database is touched, so malformed policy never reaches storage.
    Tuples are stored as JSON arrays.
    """
    unknown = sorted(set(values) - set(_POLICY_KEYS))
    if unknown:
        raise ValueError(f"unknown academy setting keys: {', '.join(unknown)}")
    if _LAB_OVERRIDE_KEY in values:
        _lab_cas_guard(db, _lab_proposal(db, values))
    coerced = {key: _coerce(key, raw, getattr(settings, key)) for key, raw in values.items()}
    candidate = _defaults()
    candidate.update(coerced)
    _expand_lab_overrides(candidate, get_all(db).keys())
    _validate_relations(candidate)
    _flush_if_autoflush(db)
    for key, value in coerced.items():
        json_value = list(value) if isinstance(value, tuple) else (value if isinstance(value, (dict, list)) else None)
        text_value = None if json_value is not None else _as_text(value)
        row_id = db.execute(
            text(
                "SELECT id FROM public.domain_settings WHERE domain = :domain "
                "AND scope_kind = :scope_kind AND tenant_id IS NULL AND key = :key"
            ).bindparams(domain=DOMAIN, scope_kind="platform", key=key)
        ).scalar_one_or_none()
        json_text = None if json_value is None else json.dumps(json_value)
        if row_id is None:
            db.execute(
                text(
                    "INSERT INTO public.domain_settings "
                    "(id, tenant_id, scope_kind, scope_id, domain, key, value_type, value_text, "
                    "value_json, is_secret, is_active) VALUES "
                    "(:id, NULL, 'platform', NULL, :domain, :key, 'string', :value_text, "
                    "CAST(:value_json AS json), false, true)"
                ).bindparams(id=uuid4(), domain=DOMAIN, key=key, value_text=text_value, value_json=json_text)
            )
        else:
            db.execute(
                text(
                    "UPDATE public.domain_settings SET value_text = :value_text, "
                    "value_json = CAST(:value_json AS json), updated_at = now() WHERE id = :id"
                ).bindparams(id=row_id, value_text=text_value, value_json=json_text)
            )
    db.flush()


def _as_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _with_env_secrets(merged: dict[str, Any]) -> dict[str, Any]:
    """Overlay env-only secrets; they are never stored settings.

    ``smtp_password`` is refused by ``set_many`` (not in ``_DEFAULTS``)
    and read solely from the product environment config.
    """
    from app.config import settings

    merged["smtp_password"] = settings.smtp_password
    return merged


def _retain_null_keys() -> frozenset[str]:
    from app import admissions_settings as v
    from app.lab_policy import LAB_CAPACITY_KEYS, LAB_EXECUTION_OVERRIDE_KEY, LAB_EXECUTION_POLICY_SHAPES

    return frozenset(
        {
            *_bank_role_validators(),
            "admin_home_access_roles",
            "lab_admin_access_roles",
            "reminders_admin_access_roles",
            "teaching_access_roles",
            "catalog_staff_roles",
            "gradebook_access_roles",
            "settings_access_roles",
            "reports_access_roles",
            "notifications_recent_limit",
            "invitation_member_role_map",
            "invitation_default_member_role",
            "web_account_creation_role_admission",
            "web_account_creation_password_min_length",
            "web_account_creation_role_provisioning",
            *v.ICAL_FEED_POLICY_KEYS,
            *v.BOOTSTRAP_POLICY_KEYS,
            *v.ENTRANCE_INVITATION_POLICY_KEYS,
            *v.ENROLLMENT_INITIAL_STATE_POLICY_KEYS,
            *v.ADMISSION_ENROLLMENT_POLICY_KEYS,
            *v.INITIAL_IDENTITY_POLICY_KEYS,
            *v.INVITATION_PURPOSE_POLICY_SHAPES,
            *v.PASSWORD_CHANGE_POLICY_SHAPES,
            *v.PROFILE_POLICY_SHAPES,
            *v.AUTHENTICATION_POLICY_SHAPES,
            *v.LEARNING_POLICY_SHAPES,
            *LAB_CAPACITY_KEYS,
            *LAB_EXECUTION_POLICY_SHAPES,
            LAB_EXECUTION_OVERRIDE_KEY,
        }
    )


def get_all(db: Session | Connection) -> dict[str, Any]:
    """Return stored platform rows as ``{key: raw value}``.

    A stored NULL policy value is malformed, never an absent override:
    NULL rows surface only for policy-family keys (which refuse them
    downstream); other NULL rows keep their historical omission
    semantics. Values are the stored JSON value when present, else text.
    """
    _flush_if_autoflush(db)
    rows = db.execute(text(_DS_SELECT).bindparams(domain=DOMAIN, scope_kind="platform")).all()
    retain_null = _retain_null_keys()
    result: dict[str, Any] = {}
    for row in rows:
        key = row.key
        raw_json = getattr(row, "value_json", None)
        value_json = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
        value_text = getattr(row, "value_text", getattr(row, "value", None))
        value = value_json if value_json is not None else value_text
        if value is None and key not in retain_null:
            continue
        result[key] = value
    return result


def delete(db: Session, key: str) -> None:
    """Delete a stored key if present. Flushes; never commits.

    Refuses when removing the override would leave relations invalid.
    """
    _flush_if_autoflush(db)
    existing = {
        row_key: row_id
        for row_id, row_key in db.execute(
            text(
                "SELECT id, key FROM public.domain_settings WHERE domain = :domain "
                "AND scope_kind = :scope_kind AND tenant_id IS NULL"
            ).bindparams(domain=DOMAIN, scope_kind="platform")
        )
    }
    if key == _LAB_OVERRIDE_KEY:
        _lab_cas_guard(db, _lab_proposal(db, {}, removed=key))
    row = existing.get(key)
    if row is None:
        return
    candidate = _defaults()
    for other, raw in get_all(db).items():
        if other != key and other in candidate:
            candidate[other] = _coerce(other, raw, candidate[other])
    if key == _LAB_OVERRIDE_KEY:
        candidate.pop(_LAB_OVERRIDE_KEY, None)
    else:
        _expand_lab_overrides(candidate, get_all(db).keys())
    _validate_relations(candidate)
    db.execute(text("DELETE FROM public.domain_settings WHERE id = :id").bindparams(id=row))
    db.flush()
