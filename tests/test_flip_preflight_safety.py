"""Hosted PostgreSQL source-guard proofs; no SSH or local database required."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url

from scripts import flip_preflight

# Isolated source-shaped tables: tenant RLS, durable presence, queue due times,
# and multiple Alembic heads reproduce the guard's actual visibility boundary.
_DDL = """
CREATE TABLE public.alembic_version (version_num varchar(64) PRIMARY KEY);
INSERT INTO public.alembic_version VALUES ('legacy_head_a'), ('legacy_head_b');
CREATE TABLE public.lab_operations (
 id uuid PRIMARY KEY, tenant_id uuid NOT NULL, state varchar(16),
 not_before timestamptz NOT NULL DEFAULT now(), claimed_by text, lease_expires_at timestamptz);
CREATE TABLE public.lab_instances (
 id uuid PRIMARY KEY, tenant_id uuid NOT NULL, status varchar(16), runtime_presence varchar(16));
CREATE TABLE public.email_outbox (
 id uuid PRIMARY KEY, tenant_id uuid NOT NULL, status varchar(20),
 available_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE public.lab_result_deliveries (
 tenant_id uuid NOT NULL, operation_id uuid NOT NULL, status varchar(16),
 next_attempt_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (tenant_id, operation_id));
ALTER TABLE public.lab_operations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.lab_operations FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON public.lab_operations USING (
 tenant_id::text = current_setting('app.current_tenant', true));
GRANT USAGE ON SCHEMA public TO app_user;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO app_user;
"""


@pytest.fixture
def source(genesis_database):
    _admin, superuser, _name = genesis_database
    name = f"academy_flip_guard_{uuid.uuid4().hex[:12]}"
    server = create_engine(superuser, isolation_level="AUTOCOMMIT")
    with server.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_engine(make_url(superuser).set(database=name))
    try:
        with engine.begin() as conn:
            conn.execute(text(_DDL))
            identifier, role = conn.execute(
                text("SELECT system_identifier::text, current_user FROM pg_control_system()")
            ).one()
        yield (
            engine,
            {
                "expected_database": name,
                "expected_system_identifier": identifier,
                "expected_heads": ["legacy_head_a", "legacy_head_b"],
                "approved_role": role,
            },
        )
    finally:
        engine.dispose()
        with server.connect() as conn:
            conn.execute(
                text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=:name"), {"name": name}
            )
            conn.execute(text(f'DROP DATABASE "{name}"'))
        server.dispose()


def _insert(engine, table, **values):
    tenant = uuid.uuid4()
    values = {"tenant_id": tenant, **values}
    values.setdefault("operation_id" if table == "lab_result_deliveries" else "id", uuid.uuid4())
    columns = ", ".join(values)
    binds = ", ".join(f":{key}" for key in values)
    with engine.begin() as conn:
        conn.execute(text(f"INSERT INTO public.{table} ({columns}) VALUES ({binds})"), values)  # noqa: S608


def test_quiet_snapshot_uses_read_only_repeatable_read(source):
    engine, expected = source
    _insert(engine, "lab_instances", status="reaped", runtime_presence="absent")
    _insert(engine, "lab_operations", state="succeeded")
    _insert(engine, "email_outbox", status="sent")
    _insert(engine, "lab_result_deliveries", status="delivered")
    observed = []

    @event.listens_for(engine, "before_cursor_execute")
    def observe(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT state,"):
            cursor.execute("SHOW transaction_read_only")
            read_only = cursor.fetchone()[0]
            cursor.execute("SHOW transaction_isolation")
            observed.append((read_only, cursor.fetchone()[0]))

    assert flip_preflight.preflight_reasons(engine, **expected) == []
    assert observed == [("on", "repeatable read")]


@pytest.mark.parametrize(
    ("key", "wrong", "reason"),
    [
        ("expected_database", "wrong_database", "database"),
        ("expected_system_identifier", "0", "cluster"),
        ("expected_heads", ["legacy_head_a"], "migration heads"),
        ("approved_role", "unapproved_role", "approved role"),
    ],
)
def test_wrong_identity_precedes_quiet_queries(source, monkeypatch, key, wrong, reason):
    engine, expected = source

    def forbidden(conn):
        pytest.fail("quiet queries executed before source identity validation")

    monkeypatch.setattr(flip_preflight, "quiet_state_reasons", forbidden)
    assert reason in flip_preflight.preflight_reasons(engine, **{**expected, key: wrong})[0]


@pytest.mark.parametrize("status", ["reaped", "error"])
@pytest.mark.parametrize("presence", ["present", "unknown", None, "future_presence"])
def test_terminal_status_does_not_prove_absent_runtime(source, status, presence):
    engine, expected = source
    _insert(engine, "lab_instances", status=status, runtime_presence=presence)
    reasons = flip_preflight.preflight_reasons(engine, **expected)
    assert any("runtime_presence" in reason for reason in reasons)


@pytest.mark.parametrize(
    ("table", "values"),
    [
        ("lab_operations", {"state": "queued"}),
        ("lab_operations", {"state": "claimed"}),
        ("lab_operations", {"state": "cancelled"}),
        ("lab_operations", {"state": None}),
        ("lab_instances", {"status": "active", "runtime_presence": "absent"}),
        ("lab_instances", {"status": "future_status", "runtime_presence": "absent"}),
        ("lab_instances", {"status": None, "runtime_presence": "absent"}),
        ("email_outbox", {"status": "pending"}),
        ("email_outbox", {"status": "future_status"}),
        ("email_outbox", {"status": None}),
        ("lab_result_deliveries", {"status": "pending"}),
        ("lab_result_deliveries", {"status": "future_status"}),
        ("lab_result_deliveries", {"status": None}),
    ],
)
def test_active_and_unrecognized_states_fail_closed(source, table, values):
    engine, expected = source
    _insert(engine, table, **values)
    assert any(table in reason for reason in flip_preflight.preflight_reasons(engine, **expected))


def test_delayed_pending_delivery_blocks(source):
    engine, expected = source
    _insert(engine, "lab_result_deliveries", status="pending")
    with engine.begin() as conn:
        conn.execute(text("UPDATE public.lab_result_deliveries SET next_attempt_at=now() + interval '7 days'"))
    assert "lab_result_deliveries pending=1" in flip_preflight.preflight_reasons(engine, **expected)


def test_cancelled_email_is_terminal_history(source):
    engine, expected = source
    _insert(engine, "email_outbox", status="cancelled")
    assert flip_preflight.preflight_reasons(engine, **expected) == []


def test_absent_delivery_table_requires_explicit_allowance(source):
    engine, expected = source
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE public.lab_result_deliveries"))
    assert any(
        "lab_result_deliveries absent" in reason for reason in flip_preflight.preflight_reasons(engine, **expected)
    )
    assert flip_preflight.preflight_reasons(engine, **expected, allow_absent_legacy_delivery_table=True) == []


def test_legacy_allowance_cannot_bypass_wrong_source_identity(source):
    engine, expected = source
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE public.lab_result_deliveries"))
    assert (
        "migration heads"
        in flip_preflight.preflight_reasons(
            engine, **{**expected, "expected_heads": ["wrong_head"]}, allow_absent_legacy_delivery_table=True
        )[0]
    )


@pytest.mark.parametrize("status", ["pending", "future_status", None])
def test_legacy_allowance_never_skips_existing_delivery_table(source, status):
    engine, expected = source
    _insert(engine, "lab_result_deliveries", status=status)
    assert any(
        "lab_result_deliveries" in reason
        for reason in flip_preflight.preflight_reasons(engine, **expected, allow_absent_legacy_delivery_table=True)
    )


def test_rls_filtered_role_cannot_report_quiet(source):
    engine, _expected = source
    _insert(engine, "lab_operations", state="claimed")
    with engine.connect() as conn, conn.begin():
        conn.execute(text("SET LOCAL ROLE app_user"))
        assert conn.execute(text("SELECT count(*) FROM public.lab_operations")).scalar_one() == 0
        conn.execute(text("SET LOCAL row_security=off"))
        assert "complete visibility" in flip_preflight.quiet_state_reasons(conn)[0]


def test_bypassrls_membership_does_not_confer_visibility(source):
    engine, _expected = source
    suffix = uuid.uuid4().hex[:12]
    bypass, member = f"flip_bypass_{suffix}", f"flip_member_{suffix}"
    # Role DDL rolls back too: no cluster-wide test roles survive the test.
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(text(f'CREATE ROLE "{bypass}" NOLOGIN BYPASSRLS'))
            conn.execute(text(f'CREATE ROLE "{member}" NOLOGIN INHERIT NOBYPASSRLS NOSUPERUSER'))
            conn.execute(text(f'GRANT "{bypass}" TO "{member}"'))
            conn.execute(text(f'SET LOCAL ROLE "{member}"'))
            assert "complete visibility" in flip_preflight.quiet_state_reasons(conn)[0]
        finally:
            transaction.rollback()


def test_search_path_shadow_cannot_hide_public_work(source):
    engine, _expected = source
    _insert(engine, "lab_operations", state="claimed")
    with engine.connect() as conn, conn.begin():
        conn.execute(text("CREATE TEMP TABLE lab_operations (state text)"))
        assert conn.execute(text("SELECT count(*) FROM lab_operations")).scalar_one() == 0
        assert "lab_operations claimed=1" in flip_preflight.quiet_state_reasons(conn)


def _args():
    return [
        "flip_preflight",
        "--dsn-env",
        "FLIP_TEST_DSN",
        "--expected-database",
        "source",
        "--expected-system-identifier",
        "123",
        "--expected-head",
        "head",
        "--approved-role",
        "approved",
    ]


def test_cli_requires_explicit_identity(monkeypatch, capsys):
    monkeypatch.setenv("FLIP_TEST_DSN", "postgresql://unused")
    with pytest.raises(SystemExit) as error:
        flip_preflight.main(["flip_preflight", "--dsn-env", "FLIP_TEST_DSN"])
    assert error.value.code == 2
    assert "QUIET" not in capsys.readouterr().out


def test_cli_connection_error_never_echoes_secret(monkeypatch, capsys):
    secret = "synthetic-secret-never-print"
    dsn = f"postgresql://operator:{secret}@invalid/source"
    monkeypatch.setenv("FLIP_TEST_DSN", dsn)

    def fail(*args, **kwargs):
        raise RuntimeError(f"connection failed {dsn}")

    monkeypatch.setattr(flip_preflight, "create_engine", fail)
    assert flip_preflight.main(_args()) == 1
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err
    assert dsn not in captured.out + captured.err
    with pytest.raises(SystemExit) as error:
        flip_preflight.main(["flip_preflight", dsn])
    assert error.value.code == 2
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err
