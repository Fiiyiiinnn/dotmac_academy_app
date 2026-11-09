"""Complete Academy flip importer (operator tool, rehearsal-proven path).

Imports a legacy public-schema Academy database into a v2 genesis
database in dependency order, inside one caller-owned destination root transaction:

1. Identity: tenants, people -> kernel Party/PartyPerson (UUIDs
   preserved), user_credentials (PBKDF2 hashes carry verbatim; the
   kernel verifies them and rehashes on login), credential policies,
   account profiles, roles + grants.
2. Learning: SnapshotV6 built by `scripts.v6_builders`, imported and
   verified by the Learning participant.
3. Product tables: explicit schema mapping and value-level verification.
4. Settings: legacy platform_settings through `settings_store.set_many`
   (strict validation; failures queue as exceptions).

Anything that cannot be represented faithfully is reported in the
exceptions list, never auto-fitted. Auth sessions are not carried
(they expire at the flip by design). The destination must be a fresh
genesis database; the source is read inside one read-only transaction
for the entire import. This is flip tooling, not runtime code.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from dotmac_learning import bank_transfer
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from scripts.product_transfer import PRODUCT_TABLES, copy_product_tables
from scripts.v6_builders import build_snapshot


class ImportRefused(ValueError):
    """Sanitized refusal: the caller must never promote this destination."""

    def __init__(self, exceptions: list[tuple[str, str, str]]):
        self.exceptions = tuple(exceptions)
        super().__init__(
            f"Academy import refused ({len(exceptions)} exception(s)); destination is not accepted; do not promote"
        )


def _refuse_if_any(exceptions: list[tuple[str, str, str]]) -> None:
    if exceptions:
        raise ImportRefused(exceptions)


def _complete_visibility(conn: Connection) -> None:
    if not conn.execute(
        text("SELECT rolsuper OR rolbypassrls FROM pg_catalog.pg_roles WHERE rolname = current_user")
    ).scalar_one():
        raise ImportRefused([("database", "-", "complete_visibility_required")])
    conn.execute(text("SET LOCAL row_security = off"))
    conn.execute(text("SET LOCAL TIME ZONE 'UTC'"))


def _fresh_destination(conn: Connection) -> None:
    ERP_TABLES = ("academy_erp_completion_checkpoints", "academy_erp_imported_completion_keys")

    tables = sorted(
        {
            "tenants",
            "tenant_domains",
            "parties",
            "party_persons",
            "roles",
            "party_role_grants",
            "user_credentials",
            "academy_account_profiles",
            "academy_credential_policies",
            *PRODUCT_TABLES,
            *ERP_TABLES,
        }
    )
    for table in tables:
        conn.execute(text(f'LOCK TABLE public."{table}" IN ACCESS EXCLUSIVE MODE'))
        if conn.execute(text(f'SELECT EXISTS (SELECT 1 FROM public."{table}")')).scalar_one():  # noqa: S608
            raise ImportRefused([(table, "-", "destination_not_fresh")])
    conn.execute(text("LOCK TABLE public.domain_settings IN ACCESS EXCLUSIVE MODE"))
    if conn.execute(text("SELECT EXISTS (SELECT 1 FROM public.domain_settings WHERE domain = 'academy')")).scalar_one():
        raise ImportRefused([("domain_settings", "-", "destination_not_fresh")])
    from scripts.v6_builders import _TABLES

    for table in (*_TABLES, "question_bank_versions"):
        conn.execute(text(f'LOCK TABLE mod_learning."{table}" IN ACCESS EXCLUSIVE MODE'))
        if conn.execute(text(f'SELECT EXISTS (SELECT 1 FROM mod_learning."{table}")')).scalar_one():  # noqa: S608
            raise ImportRefused([("mod_learning", "-", "destination_not_fresh")])


def _rows(conn: Connection, sql: str, **params: Any) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(text(sql), params).mappings()]


def _verify_identity(source: Connection, dest: Connection) -> None:
    """Compare complete mapped identity extents; never expose their values."""
    mappings = (
        (
            "tenants",
            "id, slug, name, is_active, suspended_at, deleted_at, created_at, updated_at",
            "tenants",
            "id, slug, name, is_active, suspended_at, deleted_at, created_at, updated_at",
        ),
        (
            "tenant_domains",
            "id, tenant_id, domain, verified_at, created_at, updated_at",
            "tenant_domains",
            "id, tenant_id, domain, verified_at, created_at, updated_at",
        ),
        (
            "people",
            "id, tenant_id, email, (status = 'active'), created_at, updated_at",
            "parties",
            "id, tenant_id, email, is_active, created_at, updated_at",
        ),
        ("people", "id, first_name, last_name", "party_persons", "party_id, first_name, last_name"),
        (
            "people",
            "id, tenant_id, status, avatar_path, prefs::jsonb::text, created_at, updated_at",
            "academy_account_profiles",
            "party_id, tenant_id, status, avatar_path, prefs::text, created_at, updated_at",
        ),
        (
            "roles",
            "id, tenant_id, slug, name, created_at, updated_at",
            "roles",
            "id, tenant_id, slug, name, created_at, updated_at",
        ),
        (
            "person_roles",
            "id, tenant_id, person_id, role_id, created_at, updated_at",
            "party_role_grants",
            "id, tenant_id, party_id, role_id, created_at, updated_at",
        ),
        (
            "user_credentials",
            "id, tenant_id, person_id, password_hash, created_at, updated_at",
            "user_credentials",
            "id, tenant_id, party_id, password_hash, created_at, updated_at",
        ),
        (
            "user_credentials",
            "id, tenant_id, email, failed_login_attempts, 1, false, locked_until, created_at, updated_at",
            "academy_credential_policies",
            "credential_id, tenant_id, email, failed_login_attempts, lifecycle_version, reset_required, "
            "locked_until, created_at, updated_at",
        ),
    )
    for source_table, source_fields, dest_table, dest_fields in mappings:
        expected = Counter(
            tuple(row)
            for row in source.execute(
                text(
                    f"SELECT {source_fields} FROM public.{source_table}"  # noqa: S608 - code-owned mapping
                )
            )
        )
        observed = Counter(
            tuple(row)
            for row in dest.execute(
                text(
                    f"SELECT {dest_fields} FROM public.{dest_table}"  # noqa: S608 - code-owned mapping
                )
            )
        )
        if expected != observed:
            raise ImportRefused([(dest_table, "-", "identity_value_verification_failed")])


def import_identity(source: Connection, dest: Connection, exceptions: list[tuple[str, str, str]]) -> int:
    """Copy tenants/people/credentials/roles preserving UUIDs. Returns people count."""
    tenants = _rows(
        source, "SELECT id, slug, name, is_active, suspended_at, deleted_at, created_at, updated_at FROM public.tenants"
    )
    for t in tenants:
        dest.execute(
            text(
                "INSERT INTO public.tenants (id, slug, name, is_active, suspended_at, deleted_at, "
                "created_at, updated_at) "
                "VALUES (:id, :slug, :name, :is_active, :suspended_at, :deleted_at, :created_at, :updated_at)"
            ),
            t,
        )
    # Verified custom domains decide production host resolution in the
    # kernel tenant middleware; omitting them strands the public host.
    for d in _rows(
        source, "SELECT id, tenant_id, domain, verified_at, created_at, updated_at FROM public.tenant_domains"
    ):
        dest.execute(
            text(
                "INSERT INTO public.tenant_domains (id, tenant_id, domain, verified_at, "
                "created_at, updated_at) VALUES (:id, :tenant_id, :domain, :verified_at, "
                ":created_at, :updated_at)"
            ),
            d,
        )
    people = _rows(
        source,
        "SELECT id, tenant_id, email, first_name, last_name, created_at, updated_at, "
        "status, avatar_path, prefs::text AS prefs FROM public.people",
    )
    for p in people:
        display = f"{p['first_name']} {p['last_name']}".strip() or p["email"]
        dest.execute(
            text(
                "INSERT INTO public.parties (id, tenant_id, party_type, display_name, email, is_active, "
                "created_at, updated_at) VALUES (:id, :tenant_id, 'person', :display, :email, "
                ":is_active, :created_at, :updated_at)"
            ),
            {**p, "display": display, "is_active": p["status"] == "active"},
        )
        dest.execute(
            text(
                "INSERT INTO public.party_persons (party_id, first_name, last_name) "
                "VALUES (:id, :first_name, :last_name)"
            ),
            p,
        )
        dest.execute(
            text(
                "INSERT INTO public.academy_account_profiles (party_id, tenant_id, status, avatar_path, "
                "prefs, created_at, updated_at) VALUES (:id, :tenant_id, :status, :avatar_path, "
                "CAST(:prefs AS jsonb), :created_at, :updated_at)"
            ),
            p,
        )
    roles = _rows(source, "SELECT id, tenant_id, slug, name, created_at, updated_at FROM public.roles")
    for role in roles:
        dest.execute(
            text(
                "INSERT INTO public.roles (id, tenant_id, slug, name, created_at, updated_at) "
                "VALUES (:id, :tenant_id, :slug, :name, :created_at, :updated_at)"
            ),
            role,
        )
    grants = _rows(source, "SELECT id, tenant_id, person_id, role_id, created_at, updated_at FROM public.person_roles")
    for grant in grants:
        dest.execute(
            text(
                "INSERT INTO public.party_role_grants (id, tenant_id, party_id, role_id, created_at, updated_at) "
                "VALUES (:id, :tenant_id, :person_id, :role_id, :created_at, :updated_at)"
            ),
            grant,
        )
    credentials = _rows(
        source,
        "SELECT id, tenant_id, person_id, email, password_hash, created_at, updated_at, "
        "failed_login_attempts, locked_until FROM public.user_credentials",
    )
    for c in credentials:
        dest.execute(
            text(
                "INSERT INTO public.user_credentials (id, tenant_id, party_id, password_hash, created_at, updated_at) "
                "VALUES (:id, :tenant_id, :person_id, :password_hash, :created_at, :updated_at)"
            ),
            c,
        )
        dest.execute(
            text(
                "INSERT INTO public.academy_credential_policies (credential_id, tenant_id, email, "
                "failed_login_attempts, lifecycle_version, reset_required, locked_until, "
                "created_at, updated_at) VALUES (:id, :tenant_id, :email, "
                ":failed_login_attempts, 1, false, :locked_until, :created_at, :updated_at)"
            ),
            c,
        )

    _verify_identity(source, dest)
    return len(people)


def import_product_tables(
    source: Connection, dest: Connection, exceptions: list[tuple[str, str, str]]
) -> dict[str, int]:
    """Compatibility entry point; every mismatch refuses the caller's root."""
    try:
        return copy_product_tables(source, dest, tables=PRODUCT_TABLES)
    except Exception:
        exceptions.append(("product", "-", "product_transfer_refused"))
        raise ImportRefused(exceptions) from None


def import_settings(source: Connection, dest: Connection, exceptions: list[tuple[str, str, str]]) -> int:
    """Validate and persist one complete batch in the caller's destination root."""
    import math
    import re

    from app.services import settings_store

    values = {
        r["key"]: r["value"] for r in _rows(source, "SELECT key, value FROM public.platform_settings ORDER BY key")
    }
    defaults = settings_store._defaults()
    try:
        for key, value in values.items():
            if key not in defaults or value is None:
                raise ValueError("unadjudicated setting")
            applied, _validated = settings_store._validated(key, value)
            if applied:
                continue
            default = defaults[key]
            if isinstance(default, bool) and str(value).strip().lower() not in {
                "1",
                "0",
                "true",
                "false",
                "yes",
                "no",
                "on",
                "off",
            }:
                raise ValueError("invalid boolean")
            if isinstance(default, int) and not isinstance(default, bool) and not re.fullmatch(r"[+-]?\d+", str(value)):
                raise ValueError("invalid integer")
            if isinstance(default, float) and not math.isfinite(float(value)):
                raise ValueError("invalid float")
        expected = {key: settings_store._coerce(key, value) for key, value in values.items()}
        # Releasing this savepoint does not commit the external root transaction.
        with Session(bind=dest, join_transaction_mode="create_savepoint") as db, db.begin():
            settings_store.set_many(db, values)
            observed = settings_store.effective(db, require_store=True)
            if set(settings_store.get_all(db)) != set(values):
                raise ValueError("settings key extent verification")
            if any(getattr(observed, key) != value for key, value in expected.items()):
                raise ValueError("settings verification")
    except Exception:
        exceptions.append(("platform_settings", "-", "settings_batch_refused"))
        raise ImportRefused(exceptions) from None
    return len(values)


def run_import(source_url: str, dest_url: str) -> dict[str, Any]:
    """All phases share one root; every refusal rolls the destination back."""
    from scripts.erp_transfer import import_erp_history

    exceptions: list[tuple[str, str, str]] = []
    source = create_engine(source_url, isolation_level="REPEATABLE READ", hide_parameters=True)
    dest = create_engine(dest_url, hide_parameters=True)
    phase = "preflight"
    try:
        with dest.connect() as dconn, dconn.begin(), source.connect() as sconn, sconn.begin():
            sconn.execute(text("SET TRANSACTION READ ONLY"))
            _complete_visibility(sconn)
            _complete_visibility(dconn)
            _fresh_destination(dconn)
            tenant_ids = tuple(r[0] for r in sconn.execute(text("SELECT id FROM public.tenants ORDER BY id")))
            if not tenant_ids:
                raise ImportRefused([("tenants", "-", "empty_source_extent")])
            phase = "identity"
            people = import_identity(sconn, dconn, exceptions)
            _refuse_if_any(exceptions)
            phase = "snapshot"
            snapshot, build_exceptions = build_snapshot(sconn, tenant_ids)
            exceptions.extend((table, row, "snapshot_contract_refused") for table, row, _why in build_exceptions)
            _refuse_if_any(exceptions)
            observation = bank_transfer.import_snapshot_v6(dconn, snapshot)
            bank_transfer.verify_snapshot_v6(dconn, snapshot)
            phase = "product"
            product = import_product_tables(sconn, dconn, exceptions)
            _refuse_if_any(exceptions)
            phase = "erp_history"
            erp = import_erp_history(sconn, dconn)
            phase = "settings"
            settings_applied = import_settings(sconn, dconn, exceptions)
            failed_outbox = sconn.execute(
                text("SELECT count(*) FROM public.email_outbox WHERE status = 'failed'")
            ).scalar_one()
            _refuse_if_any(exceptions)
        return {
            "tenants": [str(t) for t in tenant_ids],
            "people": people,
            "learning_inserted": {r.table: r.count for r in observation.inserted_rows},
            "product_inserted": product,
            "erp_history_inserted": erp,
            "settings_applied": settings_applied,
            "excluded_rows": {"email_outbox": {"failed": failed_outbox}},
            "exceptions": [],
            "verified": True,
        }
    except ImportRefused:
        raise
    except Exception:
        # No driver exception, bound value, source hash or credential reaches diagnostics.
        raise ImportRefused([(phase, "-", "phase_refused")]) from None
    finally:
        source.dispose()
        dest.dispose()
