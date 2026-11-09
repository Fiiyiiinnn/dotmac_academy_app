"""Hosted-only populated PostgreSQL canonical-entry mechanics canaries.

These tests require a separately provisioned EMPTY disposable database. They
exercise the installed package, report its actual version/head and every
executed source digest. Passing the current a38 chain is not future-floor
adoption, identity import, incoming-FK rebind, or runtime readiness evidence.
"""

from __future__ import annotations

import tomllib
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from dotmac_kernel.migrations import versions_dir
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError

from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from legacy_probe.fixtures.kernel_identity_entry.rehearsal import (
    COLLISIONS,
    COMPATIBILITY_SCHEMA,
    config_for,
    installed_kernel,
    plan_entry,
    relocate,
    replay_kernel,
    snapshot,
)

ROOT = Path(__file__).resolve().parents[1]


def _seed_populated_legacy(connection):
    statements = (
        "INSERT INTO tenants(id,slug,name) VALUES (:tenant,:slug,'Entry canary')",
        "INSERT INTO tenant_domains(id,tenant_id,domain) VALUES (gen_random_uuid(),:tenant,:domain)",
        "INSERT INTO people(id,tenant_id,email,first_name,last_name) VALUES (:person,:tenant,'entry@example.invalid','Entry','Canary')",
        "INSERT INTO user_credentials(id,tenant_id,person_id,email,password_hash) VALUES (gen_random_uuid(),:tenant,:person,'entry@example.invalid','synthetic-non-authenticating-fixture')",
        "INSERT INTO user_credentials(id,tenant_id,person_id,email,password_hash) VALUES (gen_random_uuid(),:tenant,:person,'alias@example.invalid','synthetic-non-authenticating-fixture')",
        "INSERT INTO auth_sessions(id,tenant_id,person_id,token_hash,expires_at) VALUES (gen_random_uuid(),:tenant,:person,repeat('0',64),now()+interval '1 hour')",
        "INSERT INTO roles(id,tenant_id,slug,name) VALUES (:role,:tenant,'learner','Learner')",
        "INSERT INTO person_roles(id,tenant_id,person_id,role_id) VALUES (gen_random_uuid(),:tenant,:person,:role)",
        "INSERT INTO audit_events(id,tenant_id,actor_person_id,action,entity_type) VALUES (gen_random_uuid(),:tenant,:person,'fixture.created','fixture')",
        "INSERT INTO courses(id,tenant_id,slug,title,discipline,source_ref) VALUES (:course,:tenant,'entry','Entry','networking','fixture')",
        "INSERT INTO course_completions(id,tenant_id,person_id,course_id) VALUES (gen_random_uuid(),:tenant,:person,:course)",
    )
    for index in range(2):
        tenant, person, role, course = (str(uuid4()) for _ in range(4))
        params = {"tenant": tenant, "person": person, "role": role, "course": course,
                  "slug": f"entry-canary-{index}", "domain": f"entry-{index}.invalid"}
        for statement in statements:
            connection.execute(text(statement), params)


@contextmanager
def entry_transaction(engine):
    with engine.connect() as connection:
        with connection.begin():
            connection.execute(text("SET LOCAL ROLE app_admin"))
            yield connection
            # All successful test runs are themselves disposable rollbacks.
            connection.get_transaction().rollback()


def public_footprint(connection):
    """Catch leftover canonical objects/functions/policies after rollback."""
    return (
        tuple(connection.execute(text("""
            SELECT c.oid,c.relname,c.relkind,c.relowner,c.relacl::text,
                   c.relrowsecurity,c.relforcerowsecurity
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='public' ORDER BY c.oid
        """)).tuples()),
        tuple(connection.execute(text("""
            SELECT p.oid,p.proname,p.proowner,p.proacl::text,pg_get_functiondef(p.oid)
            FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
            WHERE n.nspname='public' ORDER BY p.oid
        """)).tuples()),
        tuple(connection.execute(text("""
            SELECT pol.oid,pol.polrelid,pol.polname,pol.polcmd,pol.polroles,
                   pol.polqual::text,pol.polwithcheck::text
            FROM pg_policy pol JOIN pg_class c ON c.oid=pol.polrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='public' ORDER BY pol.oid
        """)).tuples()),
    )


def test_populated_entry_replays_entire_installed_kernel_and_preserves_legacy(
    legacy_engine, tmp_path, record_property,
):
    with entry_transaction(legacy_engine) as connection:
        plan = plan_entry(connection)
        assert all(count > 0 for _, count, _ in plan.catalog.rows)
        assert connection.execute(text("""
            SELECT count(*) FROM (SELECT tenant_id,person_id FROM public.user_credentials
            GROUP BY tenant_id,person_id HAVING count(*)=2) s
        """)).scalar_one() == 2
        kernel = installed_kernel(connection, tmp_path / "inspect")
        pin = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["poetry"]["dependencies"]["dotmac-kernel"]["version"]
        assert kernel.version == pin
        record_property("installed_kernel_version", kernel.version)
        record_property("installed_kernel_head", kernel.head)
        record_property("future_floor_adoption_proven", "false")
        worker_oid = connection.execute(text("SELECT oid FROM pg_roles WHERE rolname='academy_lab_worker'")).scalar_one()
        cluster_roles = tuple(connection.execute(text("""
            SELECT oid,rolname,rolsuper,rolbypassrls,rolcreaterole,rolcreatedb
            FROM pg_roles ORDER BY oid
        """)).tuples())
        relocate(connection, plan)
        assert snapshot(connection, COMPATIBILITY_SCHEMA) == plan.catalog
        applied = []
        replay_kernel(connection, tmp_path / "canonical", applied)
        assert tuple(applied) == kernel.revisions
        record_property("executed_kernel_revisions", ";".join(f"{item.revision}:{item.sha256}" for item in applied))
        assert snapshot(connection, COMPATIBILITY_SCHEMA) == plan.catalog
        assert tuple(connection.execute(text("SELECT version_num FROM public.alembic_version")).scalars()) == (kernel.head,)
        assert connection.execute(text("SELECT 'public.alembic_version'::regclass::oid")).scalar_one() != next(
            row[0] for row in plan.catalog.relations if row[1] == "alembic_version"
        )
        assert connection.execute(text("SELECT count(*) FROM public.course_completions")).scalar_one() == 2
        assert connection.execute(text("SELECT oid FROM pg_roles WHERE rolname='academy_lab_worker'")).scalar_one() == worker_oid
        assert tuple(connection.execute(text("""
            SELECT oid,rolname,rolsuper,rolbypassrls,rolcreaterole,rolcreatedb
            FROM pg_roles ORDER BY oid
        """)).tuples()) == cluster_roles
        assert connection.execute(text("""
            SELECT bool_and(relrowsecurity AND relforcerowsecurity)
            FROM pg_class WHERE oid IN ('public.parties'::regclass,
                'public.auth_sessions'::regclass,'public.user_credentials'::regclass)
        """)).scalar_one()
        assert connection.execute(text("SELECT has_table_privilege('app_user','public.parties','SELECT')")).scalar_one()
        assert not connection.execute(text("SELECT has_table_privilege('academy_lab_worker','public.parties','SELECT')")).scalar_one()
        assert connection.execute(text("SELECT has_table_privilege('academy_lab_worker','academy_identity_compat.people','SELECT')")).scalar_one()
        assert connection.execute(text("SELECT to_regprocedure('public.app_current_tenant_id()') IS NOT NULL")).scalar_one()
        assert connection.execute(text("""
            SELECT bool_and(pg_get_userbyid(c.relowner)='app_admin')
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='public' AND c.relkind='r'
        """)).scalar_one()
        _prove_canonical_tenant_isolation(connection)


def _prove_canonical_tenant_isolation(connection):
    """Independent canonical test identities; this does not import legacy IDs."""
    tenants = []
    parties = []
    for index in range(2):
        tenant, party = str(uuid4()), str(uuid4())
        tenants.append(tenant)
        parties.append(party)
        params = {"tenant": tenant, "party": party, "slug": f"canonical-{index}",
                  "email": f"canonical-{index}@example.invalid"}
        for statement in (
            "INSERT INTO public.tenants(id,slug,name) VALUES (:tenant,:slug,'Canonical fixture')",
            "INSERT INTO public.parties(id,tenant_id,party_type,display_name,email) VALUES (:party,:tenant,'person','Canonical fixture',:email)",
            "INSERT INTO public.user_credentials(id,tenant_id,party_id,password_hash) VALUES (gen_random_uuid(),:tenant,:party,'synthetic-non-authenticating-fixture')",
            "INSERT INTO public.auth_sessions(id,tenant_id,party_id,token_hash,expires_at) VALUES (gen_random_uuid(),:tenant,:party,repeat('0',64),now()+interval '1 hour')",
        ):
            connection.execute(text(statement), params)
    for role in ("app_user", "platform_api"):
        assert connection.execute(text("SELECT NOT rolsuper AND NOT rolbypassrls FROM pg_roles WHERE rolname=:role"),
                                  {"role": role}).scalar_one()
        # Role names are closed constants. Session_user is the hosted admin;
        # SET LOCAL changes effective privilege without a second connection.
        connection.execute(text(f"SET LOCAL ROLE {role}"))
        try:
            for tenant in tenants:
                connection.execute(text("SELECT set_config('app.current_tenant',:tenant,true)"), {"tenant": tenant})
                for table in ("parties", "user_credentials", "auth_sessions"):
                    sql = f"SELECT tenant_id::text FROM public.{table}"  # noqa: S608 -- closed table allowlist
                    assert tuple(connection.execute(text(sql)).scalars()) == (tenant,)
                other = next(value for value in tenants if value != tenant)
                assert connection.execute(text("UPDATE public.auth_sessions SET expires_at=expires_at WHERE tenant_id=:other"),
                                          {"other": other}).rowcount == 0
            connection.execute(text("SELECT set_config('app.current_tenant','',true)"))
            assert connection.execute(text("SELECT count(*) FROM public.parties")).scalar_one() == 0
            connection.execute(text("SELECT set_config('app.current_tenant',:tenant,true)"), {"tenant": tenants[0]})
            with pytest.raises(DBAPIError) as failure:
                with connection.begin_nested():
                    connection.execute(text("""
                        INSERT INTO public.auth_sessions(id,tenant_id,party_id,token_hash,expires_at)
                        VALUES (gen_random_uuid(),:tenant,:party,repeat('1',64),now()+interval '1 hour')
                    """), {"tenant": tenants[1], "party": parties[1]})
            assert failure.value.orig.sqlstate == "42501"
        finally:
            connection.execute(text("SET LOCAL ROLE app_admin"))


@pytest.mark.parametrize("failure_stage", ["after_relocation", "mid_kernel"])
def test_entry_failure_rolls_back_relocation_and_entire_canonical_replay(
    legacy_engine, tmp_path, failure_stage,
):
    with legacy_engine.connect() as connection:
        baseline = snapshot(connection)
        footprint = public_footprint(connection)
    with pytest.raises(RuntimeError, match="injected"):
        with entry_transaction(legacy_engine) as connection:
            relocate(connection, plan_entry(connection))
            if failure_stage == "after_relocation":
                raise RuntimeError("injected post-relocation failure")
            applied = []
            replay_kernel(connection, tmp_path / "failure", applied, fail_after=3)
    with legacy_engine.connect() as connection:
        assert snapshot(connection) == baseline
        assert public_footprint(connection) == footprint
        assert not connection.execute(text("SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname=:schema)"),
                                      {"schema": COMPATIBILITY_SCHEMA}).scalar_one()


@pytest.mark.parametrize("drift", ["version", "column", "row", "schema_contract"])
def test_entry_rejects_pre_ddl_drift(legacy_engine, drift):
    with entry_transaction(legacy_engine) as connection:
        plan = plan_entry(connection)
        if drift == "version":
            connection.execute(text("UPDATE public.alembic_version SET version_num='unexpected'"))
        elif drift == "column":
            connection.execute(text("ALTER TABLE public.people ADD COLUMN unreviewed text"))
        elif drift == "row":
            connection.execute(text("UPDATE public.people SET first_name='Changed'"))
        else:
            plan = replace(plan, compatibility_schema="public")
        with pytest.raises(ValueError, match="drift"):
            relocate(connection, plan)
        assert not connection.execute(text("SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname=:schema)"),
                                      {"schema": COMPATIBILITY_SCHEMA}).scalar_one()


@pytest.mark.parametrize("drift", ["owner", "public_grant"])
def test_plan_refuses_an_already_unsafe_owner_or_public_grant(legacy_engine, drift):
    with entry_transaction(legacy_engine) as connection:
        if drift == "owner":
            connection.execute(text("SET LOCAL ROLE postgres"))
            connection.execute(text("ALTER TABLE public.people OWNER TO postgres"))
        else:
            connection.execute(text("GRANT SELECT ON public.people TO PUBLIC"))
        with pytest.raises(ValueError, match="owner or PUBLIC grant drift"):
            plan_entry(connection)
        assert not connection.execute(text("SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname=:schema)"),
                                      {"schema": COMPATIBILITY_SCHEMA}).scalar_one()


def test_direct_canonical_root_collides_and_0003_attempts_populated_destruction(legacy_engine, tmp_path):
    """Run the real upgrade functions as negative controls, without stamping.

    The full legacy graph's incoming FKs block people DROP. The preceding
    three destructive DROP statements really execute before that failure;
    the savepoint restores them. This is not a canonical migration replay.
    """
    with entry_transaction(legacy_engine) as connection:
        baseline = snapshot(connection)
        cfg = config_for(connection, Path(versions_dir()), tmp_path / "negative", [])
        scripts = ScriptDirectory.from_config(cfg)
        for revision, sqlstate in (("0001_initial_tenant_schema", "42P07"), ("0003_party_identity", "2BP01")):
            statements = []

            def observe(conn, cursor, statement, parameters, context, executemany, captured=statements):
                if statement.lstrip().upper().startswith("DROP TABLE"):
                    captured.append(statement.strip().lower())

            event.listen(connection, "before_cursor_execute", observe)
            try:
                with pytest.raises(DBAPIError) as failure:
                    with connection.begin_nested():
                        with Operations.context(MigrationContext.configure(connection)):
                            scripts.get_revision(revision).module.upgrade()
                assert failure.value.orig.sqlstate == sqlstate
            finally:
                event.remove(connection, "before_cursor_execute", observe)
            if revision == "0003_party_identity":
                for name in ("auth_sessions", "person_roles", "user_credentials", "people"):
                    assert any(name in statement for statement in statements)
            assert snapshot(connection) == baseline


def test_exact_eight_collision_contract():
    assert set(COLLISIONS) == {
        "tenants", "tenant_domains", "people", "user_credentials",
        "auth_sessions", "roles", "person_roles", "audit_events",
    }
