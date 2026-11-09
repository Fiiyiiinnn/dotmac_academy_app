"""Explicit legacy product mapping with independent value conservation.

The recorded legacy schema is a contract, not an intersection. New source or
unexpected destination columns refuse until a reviewed mapping is committed.
Only two destination-only NULL fields and the legacy absence of the new lab
intent table are declared adaptations. ERP history has its own transfer owner.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

from sqlalchemy import text
from sqlalchemy.engine import Connection

PRODUCT_COLUMNS: dict[str, tuple[str, ...]] = {
    "lab_templates": (
        "id",
        "tenant_id",
        "course_id",
        "chapter_number",
        "activity_id",
        "slug",
        "title",
        "topology",
        "instructions_html",
        "checks",
        "seed_spec",
        "limits",
        "engine",
        "source_hash",
        "version",
        "created_at",
        "updated_at",
        "instructions_md",
        "access",
    ),
    "lab_instances": (
        "id",
        "tenant_id",
        "activity_id",
        "person_id",
        "instance_name",
        "seed",
        "status",
        "consoles",
        "error",
        "started_at",
        "last_active_at",
        "created_at",
        "updated_at",
        "runtime_presence",
    ),
    "lab_operations": (
        "id",
        "tenant_id",
        "instance_id",
        "kind",
        "state",
        "requested_by",
        "requested_at",
        "not_before",
        "claimed_by",
        "claimed_at",
        "heartbeat_at",
        "attempts",
        "finished_at",
        "last_error",
        "created_at",
        "updated_at",
        "claimed_host",
        "claimed_epoch",
        "origin",
        "runtime_precondition",
    ),
    "lab_console_activity": ("instance_id", "tenant_id", "last_seen_at"),
    "applicants": (
        "id",
        "tenant_id",
        "email",
        "first_name",
        "last_name",
        "phone",
        "program",
        "status",
        "source",
        "external_ref",
        "notes",
        "applied_on",
        "person_id",
        "created_at",
        "updated_at",
        "cohort_id",
        "assessment_score",
        "assessment_level",
        "assessment_profile",
        "assessment_taken_at",
        "assessment_token_hash",
        "assessment_started_at",
        "assessment_time_exceeded",
        "assessment_answers",
        "assessment_valid",
        "assessment_invalid_reason",
        "assessment_reset_count",
        "date_of_birth",
        "state",
        "city",
        "highest_qualification",
        "field_of_study",
        "years_experience",
        "current_role",
        "has_device",
        "has_internet",
        "can_work_at_height",
        "available_from",
        "heard_from",
        "cv_url",
        "assessment_deadline",
        "invite_sent_at",
        "assessment_elapsed_seconds",
        "onboarding_token_hash",
        "track_id",
        "assessment_bank_id",
        "assessment_return_url",
        "assessment_erp_synced_at",
        "assessment_result_version",
    ),
    "onboarding_tasks": (
        "id",
        "tenant_id",
        "applicant_id",
        "key",
        "label",
        "order_index",
        "status",
        "completed_at",
        "created_at",
        "updated_at",
    ),
    "facilitator_nominations": (
        "id",
        "tenant_id",
        "person_id",
        "nominated_by_person_id",
        "scope_type",
        "cohort_id",
        "offering_id",
        "status",
        "activated_at",
        "revoked_at",
        "activation_reason",
        "created_at",
        "updated_at",
    ),
    "facilitator_requirements": ("id", "tenant_id", "nomination_id", "course_id", "created_at", "updated_at"),
    "tenant_entrance_defaults": (
        "tenant_id",
        "default_bank_id",
        "default_time_limit_minutes",
        "created_at",
        "updated_at",
    ),
    "notifications": (
        "id",
        "tenant_id",
        "person_id",
        "kind",
        "title",
        "body",
        "link",
        "read_at",
        "created_at",
        "updated_at",
    ),
    "account_tokens": (
        "id",
        "tenant_id",
        "person_id",
        "kind",
        "token_hash",
        "expires_at",
        "used_at",
        "created_at",
        "updated_at",
    ),
    "email_outbox": (
        "id",
        "tenant_id",
        "idempotency_key",
        "kind",
        "recipient",
        "subject",
        "html_body",
        "text_body",
        "payload",
        "status",
        "attempts",
        "available_at",
        "sent_at",
        "last_error",
        "created_at",
        "updated_at",
    ),
    "reminder_preferences": (
        "id",
        "tenant_id",
        "person_id",
        "frequency",
        "quiet_start_hour",
        "quiet_end_hour",
        "optouts",
        "created_at",
        "updated_at",
    ),
    "reminder_log": (
        "id",
        "tenant_id",
        "person_id",
        "event_kind",
        "occurrence_key",
        "title",
        "link",
        "channel",
        "status",
        "outbox_key",
        "sent_at",
        "created_at",
        "updated_at",
    ),
    "lab_result_deliveries": (
        "tenant_id",
        "operation_id",
        "instance_id",
        "person_id",
        "activity_id",
        "course_id",
        "submission_id",
        "score_id",
        "certificate_id",
        "first_completion",
        "certificate_created",
        "first_pass",
        "score",
        "max_score",
        "fraction",
        "passed",
        "created_at",
        "next_attempt_at",
        "delivered_at",
        "attempts",
        "last_error",
        "status",
    ),
}

PRODUCT_TABLES = tuple(PRODUCT_COLUMNS)
DESTINATION_NULL_FIELDS = {
    "lab_operations": frozenset({"claim_lease_seconds"}),
    "applicants": frozenset({"assessment_bank_version_id"}),
}
# A legacy database without this newly introduced capability has no such rows.
# Presence/absence is inspected in the actual source catalog, never inferred
# from an incomplete saved inventory. A present table must match the full map.
OPTIONAL_SOURCE_TABLES = frozenset({"lab_result_deliveries"})


class ProductTransferRefused(ValueError):
    """Fixed-category refusal with no row values or driver diagnostics."""


def _catalog(conn: Connection, table: str) -> dict[str, str]:
    return dict(
        conn.execute(
            text(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :table ORDER BY ordinal_position"
            ),
            {"table": table},
        ).all()
    )


def _projection(columns: Iterable[str], catalog: dict[str, str]) -> str:
    fields = []
    for column in columns:
        if column not in catalog:
            fields.append(f'NULL AS "{column}"')
        elif catalog[column] in {"double precision", "real"}:
            fields.append(
                f'pg_catalog.encode(pg_catalog.float8send("{column}"::double precision), \'hex\') AS "{column}"'
            )
        elif catalog[column] in {"json", "jsonb"}:
            fields.append(f'"{column}"::jsonb::text AS "{column}"')
        else:
            fields.append(f'"{column}"::text AS "{column}"')
    return ", ".join(fields)


def copy_product_tables(
    source: Connection, dest: Connection, *, tables: Iterable[str] = PRODUCT_TABLES
) -> dict[str, int]:
    """Copy and verify fixed extents without committing the caller's root."""
    totals = {}
    for table in tables:
        if table not in PRODUCT_COLUMNS:
            raise ProductTransferRefused("product_unmapped_table")
        src = _catalog(source, table)
        dst = _catalog(dest, table)
        declared = set(PRODUCT_COLUMNS[table])
        additions = DESTINATION_NULL_FIELDS.get(table, frozenset())
        if set(dst) != declared | additions:
            raise ProductTransferRefused("product_destination_schema_drift")
        if not src and table in OPTIONAL_SOURCE_TABLES:
            if dest.execute(text(f'SELECT EXISTS (SELECT 1 FROM public."{table}")')).scalar_one():  # noqa: S608
                raise ProductTransferRefused("product_destination_not_empty")
            totals[table] = 0
            continue
        if not declared <= set(src) or set(src) - declared - additions:
            raise ProductTransferRefused("product_source_schema_drift")
        # Only nullable, explicitly named additions can be absent in the source.
        if dest.execute(text(f'SELECT EXISTS (SELECT 1 FROM public."{table}")')).scalar_one():  # noqa: S608
            raise ProductTransferRefused("product_destination_not_empty")
        for column in declared:
            compatible = src[column] == dst[column] or {src[column], dst[column]} <= {"text", "character varying"}
            compatible |= {src[column], dst[column]} <= {"json", "jsonb"}
            if not compatible:
                raise ProductTransferRefused("product_column_type_drift")
        columns = sorted(dst)
        selected = ", ".join(
            f'"{c}"::text AS "{c}"' if src[c] in {"json", "jsonb"} else f'"{c}"' for c in columns if c in src
        )
        where = " WHERE status IS DISTINCT FROM 'failed'" if table == "email_outbox" else ""
        rows = source.execute(text(f'SELECT {selected} FROM public."{table}"{where}')).mappings().all()  # noqa: S608
        trigger_disabled = table == "lab_console_activity"
        if trigger_disabled:
            state = dest.execute(
                text(
                    "SELECT tgenabled FROM pg_catalog.pg_trigger "
                    "WHERE tgrelid = 'public.lab_console_activity'::regclass "
                    "AND tgname = 'lab_console_activity_stamp' AND NOT tgisinternal"
                )
            ).scalar_one_or_none()
            if state != "O":
                raise ProductTransferRefused("product_console_stamp_not_protected")
            dest.execute(text("ALTER TABLE public.lab_console_activity DISABLE TRIGGER lab_console_activity_stamp"))
        placeholders = ", ".join(f"CAST(:{c} AS {dst[c]})" if dst[c] in {"json", "jsonb"} else f":{c}" for c in columns)
        for row in rows:
            values = {c: row.get(c) for c in columns}
            dest.execute(
                text(
                    f'INSERT INTO public."{table}" ({", ".join(chr(34) + c + chr(34) for c in columns)}) '  # noqa: S608
                    f"VALUES ({placeholders})"
                ),
                values,
            )
        if trigger_disabled:
            dest.execute(text("ALTER TABLE public.lab_console_activity ENABLE TRIGGER lab_console_activity_stamp"))
            if (
                dest.execute(
                    text(
                        "SELECT tgenabled FROM pg_catalog.pg_trigger "
                        "WHERE tgrelid = 'public.lab_console_activity'::regclass "
                        "AND tgname = 'lab_console_activity_stamp'"
                    )
                ).scalar_one()
                != "O"
            ):
                raise ProductTransferRefused("product_console_stamp_not_restored")
        # Compare every field and multiplicity, not just counts. JSON is compared
        # by PostgreSQL canonical value; float8 wire bits preserve signed zero.
        expected = Counter(
            tuple(row)
            for row in source.execute(
                text(
                    f'SELECT {_projection(columns, src)} FROM public."{table}"{where}'  # noqa: S608
                )
            )
        )
        observed = Counter(
            tuple(row)
            for row in dest.execute(
                text(
                    f'SELECT {_projection(columns, dst)} FROM public."{table}"'  # noqa: S608
                )
            )
        )
        if expected != observed:
            raise ProductTransferRefused("product_value_verification_failed")
        totals[table] = len(rows)
    return totals
