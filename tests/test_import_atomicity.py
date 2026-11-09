"""Hosted whole-import proofs using a legacy schema and a genuine genesis."""

from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from scripts import v6_import

CREATED = datetime(2024, 2, 3, 4, 5, 6, 123456, tzinfo=UTC)
UPDATED = datetime(2025, 6, 7, 8, 9, 10, 654321, tzinfo=UTC)
_TYPES = {
    "uuid": "uuid",
    "character varying": "text",
    "text": "text",
    "integer": "integer",
    "double precision": "double precision",
    "boolean": "boolean",
    "jsonb": "jsonb",
    "timestamp with time zone": "timestamptz",
    "date": "date",
}


def _legacy_schema():
    columns = defaultdict(list)
    inventory = Path(__file__).resolve().parents[1] / "legacy_probe" / "prod_columns.txt"
    for line in inventory.read_text().splitlines():
        table, column, data_type, _nullable = line.split("|")
        columns[table].append(f'"{column}" {_TYPES[data_type]}')
    return "\n".join(
        f'CREATE TABLE public."{table}" ({", ".join(fields)});' for table, fields in sorted(columns.items())
    )


@pytest.fixture
def whole_import(genesis_database):
    _admin, superuser, destination_name = genesis_database
    name = f"academy_import_source_{uuid4().hex[:12]}"
    server = create_engine(superuser, isolation_level="AUTOCOMMIT")
    source = destination = None
    try:
        with server.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
            conn.execute(text(f'GRANT CONNECT ON DATABASE "{name}" TO app_user'))
        source_url = make_url(superuser).set(database=name).render_as_string(hide_password=False)
        destination_url = make_url(superuser).set(database=destination_name).render_as_string(hide_password=False)
        source = create_engine(source_url)
        destination = create_engine(destination_url)
        ids = {key: uuid4() for key in ("tenant", "person", "role", "grant", "notification", "credential")}
        values = {**ids, "created": CREATED, "updated": UPDATED, "slug": ids["tenant"].hex}
        with source.begin() as conn:
            conn.execute(text(_legacy_schema()))
            conn.execute(
                text(
                    "INSERT INTO public.tenants (id, slug, name, is_active, created_at, updated_at) "
                    "VALUES (:tenant, :slug, 'Imported tenant', true, :created, :updated)"
                ),
                values,
            )
            conn.execute(
                text(
                    "INSERT INTO public.people "
                    "(id, tenant_id, email, first_name, last_name, status, prefs, created_at, updated_at) "
                    "VALUES (:person, :tenant, 'import-proof@example.invalid', 'Import', 'Proof', 'active', "
                    '\'{"timezone":"UTC"}\'::jsonb, :created, :updated)'
                ),
                values,
            )
            conn.execute(
                text(
                    "INSERT INTO public.roles (id, tenant_id, slug, name, created_at, updated_at) "
                    "VALUES (:role, :tenant, 'import-proof', 'Imported role', :created, :updated)"
                ),
                values,
            )
            conn.execute(
                text(
                    "INSERT INTO public.person_roles (id, tenant_id, person_id, role_id, created_at, updated_at) "
                    "VALUES (:grant, :tenant, :person, :role, :created, :updated)"
                ),
                values,
            )
            conn.execute(
                text(
                    "INSERT INTO public.user_credentials "
                    "(id, tenant_id, person_id, email, password_hash, failed_login_attempts, created_at, updated_at) "
                    "VALUES (:credential, :tenant, :person, 'import-proof@example.invalid', "
                    "'!synthetic-disabled-login', 2, :created, :updated)"
                ),
                values,
            )
            conn.execute(
                text(
                    "INSERT INTO public.notifications "
                    "(id, tenant_id, person_id, kind, title, body, created_at, updated_at) "
                    "VALUES (:notification, :tenant, :person, 'info', 'Imported notice', 'History', :created, :updated)"
                ),
                values,
            )
            conn.execute(
                text("INSERT INTO public.platform_settings (key, value) VALUES ('branding_name', 'Imported Academy')")
            )
        yield source, destination, source_url, destination_url, ids
    finally:
        if source is not None:
            source.dispose()
        if destination is not None:
            destination.dispose()
        with server.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :name AND pid <> pg_backend_pid()"
                ),
                {"name": name},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        server.dispose()


def _assert_empty(destination):
    with destination.connect() as conn:
        for table in (
            "public.tenants",
            "public.parties",
            "public.party_persons",
            "public.roles",
            "public.party_role_grants",
            "public.user_credentials",
            "public.academy_account_profiles",
            "public.academy_credential_policies",
            "public.notifications",
            "public.academy_erp_completion_checkpoints",
            "public.academy_erp_imported_completion_keys",
            "mod_learning.courses",
            "mod_learning.course_completions",
        ):
            assert conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 0  # noqa: S608
        assert (
            conn.execute(text("SELECT count(*) FROM public.domain_settings WHERE domain = 'academy'")).scalar_one() == 0
        )


def test_run_import_preserves_identity_roles_grants_product_and_settings(whole_import):
    _source, destination, source_url, destination_url, ids = whole_import
    report = v6_import.run_import(source_url, destination_url)
    assert report["verified"] is True
    assert report["exceptions"] == []
    assert report["people"] == 1
    assert report["settings_applied"] == 1
    assert report["product_inserted"]["notifications"] == 1
    assert all(count == 0 for count in report["learning_inserted"].values())
    with destination.connect() as conn:
        for table, key in (
            ("tenants", "tenant"),
            ("parties", "person"),
            ("roles", "role"),
            ("party_role_grants", "grant"),
            ("user_credentials", "credential"),
            ("notifications", "notification"),
        ):
            assert conn.execute(
                text(f"SELECT id, created_at, updated_at FROM public.{table}"),  # noqa: S608
            ).one() == (ids[key], CREATED, UPDATED)
        assert conn.execute(text("SELECT tenant_id, party_id, role_id FROM public.party_role_grants")).one() == (
            ids["tenant"],
            ids["person"],
            ids["role"],
        )
        assert conn.execute(text("SELECT slug, name FROM public.roles")).one() == ("import-proof", "Imported role")
        assert conn.execute(text("SELECT first_name, last_name FROM public.party_persons")).one() == ("Import", "Proof")
        assert (
            conn.execute(
                text("SELECT value_text FROM public.domain_settings WHERE domain = 'academy' AND key = 'branding_name'")
            ).scalar_one()
            == "Imported Academy"
        )


def test_invalid_product_row_rolls_back_the_complete_import(whole_import):
    source, destination, source_url, destination_url, ids = whole_import
    with source.begin() as conn:
        conn.execute(
            text("UPDATE public.notifications SET title = NULL WHERE id = :id"),
            {"id": ids["notification"]},
        )
    with pytest.raises(v6_import.ImportRefused) as refusal:
        v6_import.run_import(source_url, destination_url)
    assert refusal.value.exceptions == (("product", "-", "product_transfer_refused"),)
    _assert_empty(destination)


@pytest.mark.parametrize(
    "key,value", [("unreviewed_import_setting", "value"), ("max_concurrent_labs", "not-an-integer")]
)
def test_late_invalid_setting_rolls_back_identity_and_product_rows(whole_import, key, value):
    source, destination, source_url, destination_url, _ids = whole_import
    with source.begin() as conn:
        conn.execute(
            text("INSERT INTO public.platform_settings (key, value) VALUES (:key, :value)"),
            {"key": key, "value": value},
        )
    with pytest.raises(v6_import.ImportRefused) as refusal:
        v6_import.run_import(source_url, destination_url)
    assert refusal.value.exceptions == (("platform_settings", "-", "settings_batch_refused"),)
    _assert_empty(destination)


def test_snapshot_builder_exception_rolls_back_prior_identity_writes(whole_import, monkeypatch):
    _source, destination, source_url, destination_url, _ids = whole_import
    real_builder = v6_import.build_snapshot

    def refused_snapshot(source, tenant_ids):
        snapshot, exceptions = real_builder(source, tenant_ids)
        assert exceptions == []
        return snapshot, [("courses", "-", "synthetic contract failure")]

    monkeypatch.setattr(v6_import, "build_snapshot", refused_snapshot)
    with pytest.raises(v6_import.ImportRefused) as refusal:
        v6_import.run_import(source_url, destination_url)
    assert refusal.value.exceptions == (("courses", "-", "snapshot_contract_refused"),)
    _assert_empty(destination)


def test_nonfresh_destination_refuses_and_preserves_existing_rows(whole_import):
    _source, destination, source_url, destination_url, _ids = whole_import
    existing = uuid4()
    with destination.begin() as conn:
        conn.execute(
            text("INSERT INTO public.tenants (id, slug, name) VALUES (:id, :slug, 'Existing tenant')"),
            {"id": existing, "slug": existing.hex},
        )
    with pytest.raises(v6_import.ImportRefused) as refusal:
        v6_import.run_import(source_url, destination_url)
    assert refusal.value.exceptions[0][2] == "destination_not_fresh"
    with destination.connect() as conn:
        assert conn.execute(text("SELECT id FROM public.tenants")).scalars().all() == [existing]
        assert conn.execute(text("SELECT count(*) FROM public.parties")).scalar_one() == 0


def test_filtered_app_user_source_refuses_before_import(whole_import):
    source, destination, source_url, destination_url, _ids = whole_import
    with source.begin() as conn:
        conn.execute(text("GRANT USAGE ON SCHEMA public TO app_user"))
        conn.execute(text("GRANT SELECT ON ALL TABLES IN SCHEMA public TO app_user"))
        conn.execute(text("ALTER TABLE public.tenants ENABLE ROW LEVEL SECURITY"))
        conn.execute(text("ALTER TABLE public.tenants FORCE ROW LEVEL SECURITY"))
        conn.execute(text("CREATE POLICY hidden_source_tenants ON public.tenants USING (false)"))
    filtered_url = make_url(source_url).set(username="app_user", password=None).render_as_string(hide_password=False)
    filtered = create_engine(filtered_url)
    try:
        with filtered.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM public.tenants")).scalar_one() == 0
        with pytest.raises(v6_import.ImportRefused) as refusal:
            v6_import.run_import(filtered_url, destination_url)
        assert refusal.value.exceptions == (("database", "-", "complete_visibility_required"),)
    finally:
        filtered.dispose()
    _assert_empty(destination)


def test_source_transaction_finalization_failure_rolls_back_destination(whole_import, monkeypatch):
    _source, destination, source_url, destination_url, _ids = whole_import
    real_create_engine = v6_import.create_engine
    source_finalized = []

    class SourceConnection:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        @contextmanager
        def begin(self):
            with self.connection.begin() as transaction:
                yield transaction
            # All import phases succeeded and the actual read-only source root
            # finalized. Destination must still be inside its own root here.
            source_finalized.append(True)
            raise RuntimeError("synthetic source finalization failure")

    class SourceEngine:
        def __init__(self, engine):
            self.engine = engine

        @contextmanager
        def connect(self):
            with self.engine.connect() as connection:
                yield SourceConnection(connection)

        def dispose(self):
            self.engine.dispose()

    def instrument_source(url, **kwargs):
        engine = real_create_engine(url, **kwargs)
        return SourceEngine(engine) if make_url(url) == make_url(source_url) else engine

    monkeypatch.setattr(v6_import, "create_engine", instrument_source)
    with pytest.raises(v6_import.ImportRefused) as refusal:
        v6_import.run_import(source_url, destination_url)
    assert source_finalized == [True]
    assert refusal.value.exceptions == (("settings", "-", "phase_refused"),)
    _assert_empty(destination)


def test_late_settings_trigger_cannot_expand_verified_key_extent(whole_import):
    _source, destination, source_url, destination_url, _ids = whole_import
    with destination.begin() as conn:
        conn.execute(
            text(
                "CREATE FUNCTION public.inject_extra_import_setting() RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN IF NEW.domain = 'academy' AND NEW.key = 'branding_name' THEN "
                "INSERT INTO public.domain_settings "
                "(id, tenant_id, scope_kind, scope_id, domain, key, value_type, value_text, "
                "value_json, is_secret, is_active) VALUES "
                "(gen_random_uuid(), NULL, 'platform', NULL, 'academy', 'smtp_host', 'string', "
                "'injected.invalid', NULL, false, true); "
                "END IF; RETURN NEW; END $$"
            )
        )
        conn.execute(
            text(
                "CREATE TRIGGER inject_extra_import_setting AFTER INSERT ON public.domain_settings "
                "FOR EACH ROW EXECUTE FUNCTION public.inject_extra_import_setting()"
            )
        )
    with pytest.raises(v6_import.ImportRefused) as refusal:
        v6_import.run_import(source_url, destination_url)
    assert refusal.value.exceptions == (("platform_settings", "-", "settings_batch_refused"),)
    _assert_empty(destination)
