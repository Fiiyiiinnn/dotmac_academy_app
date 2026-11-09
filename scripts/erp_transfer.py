"""Conserve legacy ERP acknowledgement facts in the caller's import transaction."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError


class ERPHistoryImportError(ValueError):
    """Sanitized, fixed-category refusal; never includes source row values."""


def _columns(source: Connection, table: str) -> set[str]:
    return set(
        source.execute(
            text(
                "SELECT a.attname FROM pg_catalog.pg_attribute a "
                "JOIN pg_catalog.pg_class c ON c.oid = a.attrelid "
                "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relname = :table "
                "AND a.attnum > 0 AND NOT a.attisdropped"
            ),
            {"table": table},
        ).scalars()
    )


def import_erp_history(source: Connection, dest: Connection) -> dict[str, int]:
    """Copy every completion's ACK and immutable import membership, then verify.

    Both connections must already be in caller-owned root transactions with
    full row visibility. Learning completions must have been imported first.
    Neither connection is committed, rolled back, or given a nested transaction.
    A refusal requires the caller to roll back its destination transaction.

    The independent legacy marker table can supply a timestamp absent from the
    completion. Two non-NULL timestamps must agree. This compatible merge never
    invents a percentage: the completion's raw percentage, including NULL, wins
    because the legacy marker schema has no percentage fact.
    """
    if not source.in_transaction() or not dest.in_transaction():
        raise ERPHistoryImportError("erp_history_transaction_required")
    try:
        return _import_erp_history(source, dest)
    except SQLAlchemyError:
        raise ERPHistoryImportError("erp_history_database_refusal") from None


def _import_erp_history(source: Connection, dest: Connection) -> dict[str, int]:
    required = {"tenant_id", "id", "erp_synced_at", "erp_synced_pct"}
    if not required.issubset(_columns(source, "course_completions")):
        raise ERPHistoryImportError("erp_history_source_schema")
    marker_columns = _columns(source, "academy_erp_completion_checkpoints")
    if marker_columns != {"tenant_id", "completion_id", "erp_synced_at"}:
        raise ERPHistoryImportError("erp_history_source_schema")

    # These are whole import extents, including tenants with only NULL ACKs.
    if dest.execute(text("SELECT EXISTS (SELECT 1 FROM public.academy_erp_completion_checkpoints)")).scalar_one():
        raise ERPHistoryImportError("erp_history_destination_not_empty")
    if dest.execute(text("SELECT EXISTS (SELECT 1 FROM public.academy_erp_imported_completion_keys)")).scalar_one():
        raise ERPHistoryImportError("erp_history_destination_not_empty")

    rows = (
        source.execute(
            text(
                "SELECT tenant_id, id AS completion_id, erp_synced_at, erp_synced_pct, "
                "pg_catalog.float8send(erp_synced_pct) AS pct_bits "
                "FROM public.course_completions"
            )
        )
        .mappings()
        .all()
    )
    expected = {}
    for row in rows:
        key = (row["tenant_id"], row["completion_id"])
        if None in key or key in expected:
            raise ERPHistoryImportError("erp_history_source_keys")
        expected[key] = dict(row)
    seen_markers = set()
    markers = source.execute(
        text("SELECT tenant_id, completion_id, erp_synced_at FROM public.academy_erp_completion_checkpoints")
    ).mappings()
    for marker in markers:
        key = (marker["tenant_id"], marker["completion_id"])
        if key not in expected:
            raise ERPHistoryImportError("erp_history_orphan_marker")
        if key in seen_markers:
            raise ERPHistoryImportError("erp_history_source_keys")
        seen_markers.add(key)
        completion_at = expected[key]["erp_synced_at"]
        marker_at = marker["erp_synced_at"]
        if completion_at is not None and marker_at is not None and completion_at != marker_at:
            raise ERPHistoryImportError("erp_history_conflicting_ack")
        if completion_at is None:
            expected[key]["erp_synced_at"] = marker_at

    learning_keys = set(dest.execute(text("SELECT tenant_id, id FROM mod_learning.course_completions")))
    if learning_keys != set(expected):
        raise ERPHistoryImportError("erp_history_completion_coverage")
    if expected:
        dest.execute(
            text(
                "INSERT INTO public.academy_erp_completion_checkpoints "
                "(tenant_id, completion_id, erp_synced_at, erp_synced_pct) "
                "VALUES (:tenant_id, :completion_id, :erp_synced_at, :erp_synced_pct)"
            ),
            list(expected.values()),
        )
        dest.execute(
            text(
                "INSERT INTO public.academy_erp_imported_completion_keys (tenant_id, completion_id) "
                "VALUES (:tenant_id, :completion_id)"
            ),
            [{"tenant_id": key[0], "completion_id": key[1]} for key in expected],
        )
    # A separate read checks actual persisted values, including signed-zero bits
    # and NULLs. Driver adaptation or trigger changes cannot silently pass.
    actual = {
        (row[0], row[1]): (row[2], row[3])
        for row in dest.execute(
            text(
                "SELECT tenant_id, completion_id, erp_synced_at, "
                "pg_catalog.float8send(erp_synced_pct) "
                "FROM public.academy_erp_completion_checkpoints"
            )
        )
    }
    facts = {key: (row["erp_synced_at"], row["pct_bits"]) for key, row in expected.items()}
    if actual != facts:
        raise ERPHistoryImportError("erp_history_ack_verification")
    members = set(
        dest.execute(text("SELECT tenant_id, completion_id FROM public.academy_erp_imported_completion_keys"))
    )
    if members != set(expected):
        raise ERPHistoryImportError("erp_history_membership_verification")
    return {
        "academy_erp_completion_checkpoints": len(expected),
        "academy_erp_imported_completion_keys": len(expected),
    }
