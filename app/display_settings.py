"""Display setting specs the kernel web dependencies resolve on every request.

The kernel warms the ``display`` settings (timezone, date and datetime format)
for every authenticated web request but ships no ``SettingSpec`` for them, so a
product must register them. The definitions are copied field for field from the
Starter app (``app/features/settings/spec.py``), including defaults and
validators.

Importing this module registers the specs in the kernel's process-global
registry as a side effect; ``app.assembly`` imports it. This is an import-time
registry only: it performs no DDL and no database access.
"""

# Ported at new-composition genesis (composition wave).

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotmac_kernel.settings_models import SettingDomain, SettingValueType
from dotmac_kernel.settings_resolver import SettingSpec, register_specs


def _validate_timezone(value: object) -> None:
    try:
        ZoneInfo(str(value))
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown IANA timezone: {value!r}") from exc


# Documented, Python-portable strftime directive characters (i.e. the ones
# whose behavior isn't left to the underlying platform C library — see
# https://docs.python.org/3/library/datetime.html#strftime-and-strptime-format-codes).
# `%` itself is the `%%` escape, handled separately below (not a "real"
# directive — it doesn't count toward the "at least one directive" rule).
_PORTABLE_DIRECTIVES = frozenset("aAbBcdfGHIjmMpSuUVwWxXyYzZ%")


def _validate_strftime(value: object) -> None:
    """Explicit tokenizer, not a substring-check + probe-render.

    `datetime.strftime` delegates any directive it doesn't recognize to the
    platform C library, which makes a naive `"%" in fmt` + try/except probe
    unreliable: on glibc, an unknown directive like `%Q` passes through as
    literal text with NO exception, `%%` (the literal-`%` escape) satisfies
    a bare substring check while containing zero real directives, and a
    dangling trailing `%` renders as a literal `%` with no exception too.
    This walks the string directive-by-directive instead, so acceptance
    never depends on what the host libc happens to do with an unknown code.
    """
    fmt = str(value)
    if "\x00" in fmt:
        raise ValueError("format string must not contain null bytes")
    real_directive_count = 0
    i = 0
    length = len(fmt)
    while i < length:
        if fmt[i] != "%":
            i += 1
            continue
        if i + 1 >= length:
            raise ValueError("dangling '%' at end of format string")
        directive = fmt[i + 1]
        if directive not in _PORTABLE_DIRECTIVES:
            raise ValueError(f"unsupported directive %{directive}")
        if directive != "%":
            real_directive_count += 1
        i += 2
    if real_directive_count == 0:
        raise ValueError("format must contain at least one strftime % directive")
    # Belt-and-braces: the tokenizer above is the real gate, but still probe
    # -render against a fixed reference datetime in case some accepted
    # directive combination is otherwise malformed.
    try:
        datetime(2026, 1, 31, 13, 45, tzinfo=UTC).strftime(fmt)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"invalid strftime format: {exc}") from exc


DISPLAY_SPECS: list[SettingSpec] = [
    SettingSpec(
        domain=SettingDomain.display,
        key="timezone",
        value_type=SettingValueType.string,
        default="UTC",
        label="Display timezone (IANA name, e.g. Europe/London)",
        description=(
            "Timezone the admin portal renders timestamps in. The JSON API is "
            "unaffected: it always returns ISO-8601 UTC."
        ),
        validator=_validate_timezone,
    ),
    SettingSpec(
        domain=SettingDomain.display,
        key="date_format",
        value_type=SettingValueType.string,
        default="%Y-%m-%d",
        label="Date display format (strftime)",
        description="strftime format for dates without a time, e.g. %Y-%m-%d.",
        validator=_validate_strftime,
    ),
    SettingSpec(
        domain=SettingDomain.display,
        key="datetime_format",
        value_type=SettingValueType.string,
        default="%Y-%m-%d %H:%M",
        label="Date+time display format (strftime)",
        description="strftime format for dates with a time, e.g. %Y-%m-%d %H:%M.",
        validator=_validate_strftime,
    ),
]

register_specs(DISPLAY_SPECS)
