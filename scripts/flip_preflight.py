"""Fail-closed source identity and quiet-state proof for a downtime snapshot.

Use --dsn-env NAME for a protected environment-supplied DSN. All expected
identity values and the approved complete-visibility role are mandatory.
Stopping workers, web writers and schedulers remains an operator prerequisite;
this read-only snapshot cannot prove that external processes stay stopped.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Collection

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine


def _visibility_reason(conn: Connection) -> str | None:
    # SUPERUSER/BYPASSRLS are attributes of the effective role, not inherited
    # membership privileges. Table ownership and row_security=off are insufficient
    # for FORCE ROW LEVEL SECURITY tables.
    visible = conn.execute(
        text("SELECT rolsuper OR rolbypassrls FROM pg_catalog.pg_roles WHERE rolname = current_user")
    ).scalar_one()
    if not visible:
        return "effective role must have SUPERUSER or BYPASSRLS for complete visibility"
    return None


def quiet_state_reasons(conn: Connection, *, allow_absent_legacy_delivery_table: bool = False) -> list[str]:
    """Return blocking database facts; caller owns the read-only snapshot.

    Direct callers still require complete visibility and own source identity
    validation. Missing tables/columns never silently become a quiet result.
    The explicit legacy allowance applies only to a proven absent delivery table.
    """
    visibility = _visibility_reason(conn)
    if visibility:
        return [visibility]
    reasons: list[str] = []
    delivery_table_absent = conn.execute(
        text("SELECT pg_catalog.to_regclass('public.lab_result_deliveries') IS NULL")
    ).scalar_one()
    if delivery_table_absent and not allow_absent_legacy_delivery_table:
        reasons.append("lab_result_deliveries absent; explicit reviewed legacy allowance required")
    # Terminal vocabulary comes from Academy's models/services. NULL and any
    # future/unrecognized value block until explicitly reviewed.
    for table, column, terminal in (
        ("lab_operations", "state", {"succeeded", "failed"}),
        ("lab_instances", "status", {"reaped", "error"}),
        ("email_outbox", "status", {"sent", "failed", "cancelled"}),
        ("lab_result_deliveries", "status", {"delivered", "failed"}),
    ):
        if table == "lab_result_deliveries" and delivery_table_absent:
            continue
        # Identifiers are code-owned constants; row values never become SQL or
        # diagnostics (an unexpected value may itself contain sensitive data).
        rows = conn.execute(
            text(f"SELECT {column}, count(*) FROM public.{table} GROUP BY {column}")  # noqa: S608
        ).all()
        known_blocking = {"queued", "claimed", "active", "provisioning", "resetting", "pending"}
        for value, count in rows:
            if value not in terminal:
                label = value if value in known_blocking else "unknown/NULL"
                reasons.append(f"{table} {label}={count}")
    rows = conn.execute(
        text(
            "SELECT runtime_presence, count(*) FROM public.lab_instances "
            "WHERE runtime_presence IS DISTINCT FROM 'absent' GROUP BY runtime_presence"
        )
    ).all()
    for presence, count in rows:
        label = presence if presence in {"present", "unknown"} else "unknown/NULL"
        reasons.append(f"lab_instances runtime_presence {label}={count}")
    return reasons


def preflight_reasons(
    engine: Engine,
    *,
    expected_database: str,
    expected_system_identifier: str,
    expected_heads: Collection[str],
    approved_role: str,
    allow_absent_legacy_delivery_table: bool = False,
) -> list[str]:
    """Validate pinned identity before reading any quiet-state tables."""
    if not all((expected_database, expected_system_identifier, expected_heads, approved_role)):
        raise ValueError("all source identity expectations are required")
    if any(not head for head in expected_heads):
        raise ValueError("migration heads cannot be empty")
    with engine.connect() as conn, conn.begin():
        conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        conn.execute(text("SET LOCAL search_path = pg_catalog"))
        database, role = conn.execute(text("SELECT current_database(), current_user")).one()
        if database != expected_database:
            return ["source database does not match approved identity"]
        if role != approved_role:
            return ["effective role does not match approved role"]
        visibility = _visibility_reason(conn)
        if visibility:
            return [visibility]
        identifier = conn.execute(
            text("SELECT system_identifier::text FROM pg_catalog.pg_control_system()")
        ).scalar_one()
        if identifier != expected_system_identifier:
            return ["source cluster does not match approved system_identifier"]
        heads = set(conn.execute(text("SELECT version_num FROM public.alembic_version")).scalars())
        if heads != set(expected_heads):
            return ["source migration heads do not match approved heads"]
        return quiet_state_reasons(conn, allow_absent_legacy_delivery_table=allow_absent_legacy_delivery_table)


class _SafeParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # argparse normally echoes unrecognized arguments, potentially a DSN.
        self.print_usage(sys.stderr)
        self.exit(2, "invalid arguments; use --help (DSNs must come from --dsn-env)\n")


def main(argv: list[str]) -> int:
    parser = _SafeParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--dsn-env", required=True, help="name of environment variable containing source DSN")
    parser.add_argument("--expected-database", required=True)
    parser.add_argument("--expected-system-identifier", required=True)
    parser.add_argument("--expected-head", action="append", required=True)
    parser.add_argument("--approved-role", required=True)
    parser.add_argument(
        "--allow-absent-legacy-delivery-table",
        action="store_true",
        help="explicit receipt-backed allowance for a proven absent legacy delivery table after identity validation",
    )
    args = parser.parse_args(argv[1:])
    dsn = os.environ.get(args.dsn_env)
    if not dsn:
        print("REFUSED - source DSN environment variable is missing or empty")
        return 2
    engine = None
    try:
        engine = create_engine(dsn, hide_parameters=True)
        reasons = preflight_reasons(
            engine,
            expected_database=args.expected_database,
            expected_system_identifier=args.expected_system_identifier,
            expected_heads=args.expected_head,
            approved_role=args.approved_role,
            allow_absent_legacy_delivery_table=args.allow_absent_legacy_delivery_table,
        )
    except Exception:
        # Driver/SQLAlchemy exceptions can embed passwords, DSNs or SQL values.
        print("REFUSED - unable to prove source identity and quiet state; inspect protected diagnostics")
        return 1
    finally:
        if engine is not None:
            engine.dispose()
    if reasons:
        print("NOT QUIET - snapshot refused:")
        for reason in reasons:
            print(f"  - {reason}")
        return 1
    print("QUIET - approved source snapshot is quiet; writer shutdown remains an operator prerequisite")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
