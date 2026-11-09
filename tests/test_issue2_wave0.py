"""Issue #2 wave-0 proofs: worker ACL, lab-policy CAS, backfill, identity guards.

Covers disposition rows services/test_lab_worker_privileges.py (ACL
subset), services/test_settings_store_fresh_policy.py (CAS contract),
test_backfill.py, and the SUPERSEDED evidence gaps for
test_academy_credential_reset_state.py (23502) and
test_kernel_identity_entry.py (cross-tenant 42501/RLS).
"""

from __future__ import annotations

import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker


def test_lab_worker_acl_exact_grants(genesis_database):
    admin, _superuser, _name = genesis_database
    with admin.connect() as conn:

        def has(table, priv):
            return conn.execute(
                text("SELECT has_table_privilege('academy_lab_worker', :t, :p)"),
                {"t": f"public.{table}", "p": priv},
            ).scalar_one()

        assert has("lab_operations", "SELECT")
        assert has("lab_operations", "INSERT")
        assert has("lab_operations", "UPDATE")
        assert not has("lab_operations", "DELETE")
        assert has("domain_settings", "SELECT")
        assert not has("domain_settings", "UPDATE")
        for forbidden in ("email_outbox", "notifications", "parties"):
            assert not has(forbidden, "SELECT"), forbidden
            assert not has(forbidden, "INSERT"), forbidden
        facilitator = conn.execute(
            text(
                "SELECT has_function_privilege('academy_lab_worker', "
                "'public.activate_eligible_facilitator_nominations(uuid,uuid,uuid,uuid,integer,uuid)', 'EXECUTE')"
            )
        ).scalar_one()
        assert not facilitator
        capability = conn.execute(
            text("SELECT proacl::text FROM pg_proc WHERE proname = 'record_claimed_lab_check'")
        ).scalar_one()
        assert "academy_lab_worker=X" in capability
        memberships = conn.execute(
            text(
                "SELECT count(*) FROM pg_auth_members am JOIN pg_roles m ON m.oid = am.member "
                "WHERE m.rolname = 'academy_lab_worker'"
            )
        ).scalar_one()
        assert memberships == 0


@pytest.mark.parametrize(
    "initial,second_action",
    [
        ('{"lab_worker_poll_interval_seconds":3}', "set"),
        ("ABSENT", "set"),
        ('{"lab_worker_poll_interval_seconds":3}', "delete"),
    ],
)
def test_lab_policy_cas_exactly_one_winner(genesis_database, monkeypatch, initial, second_action):
    from app.services import settings_store

    admin, _superuser, _name = genesis_database
    factory = sessionmaker(bind=admin, autoflush=False)
    key = "lab_execution_policy_overrides"
    with factory() as db:
        if initial != "ABSENT":
            settings_store.set_many(db, {key: initial})
            db.commit()
    barrier = Barrier(2)
    original = settings_store._lab_proposal

    def observed(session, updates, *, removed=None):
        result = original(session, updates, removed=removed)
        barrier.wait(timeout=15)
        return result

    monkeypatch.setattr(settings_store, "_lab_proposal", observed)

    def writer(index):
        with factory() as session:
            try:
                if index == 1 and second_action == "delete":
                    settings_store.delete(session, key)
                else:
                    settings_store.set_many(
                        session,
                        {
                            key: json.dumps({"lab_worker_poll_interval_seconds": index + 7}),
                            "branding_name": f"winner-{index}",
                        },
                    )
                assert session.in_transaction()
                session.commit()
                return index, "won"
            except settings_store.LabExecutionPolicyConflict:
                assert session.in_transaction()
                session.rollback()
                return index, "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [f.result(timeout=30) for f in (pool.submit(writer, 0), pool.submit(writer, 1))]
    monkeypatch.setattr(settings_store, "_lab_proposal", original)
    assert sorted(r for _, r in results) == ["conflict", "won"]
    winner = next(i for i, r in results if r == "won")
    with factory() as db:
        branding = settings_store.get_all(db).get("branding_name")
    expected = None if (winner == 1 and second_action == "delete") else f"winner-{winner}"
    assert branding == expected


def test_backfill_imports_and_is_idempotent(genesis_database):
    from scripts.backfill_applicants import backfill

    admin, _superuser, _name = genesis_database
    tenant = uuid.uuid4()
    with admin.begin() as conn:
        conn.execute(
            text("INSERT INTO tenants (id, slug, name) VALUES (:id, :s, 'BF')"),
            {"id": tenant, "s": tenant.hex},
        )
    with Session(admin) as db:
        rows = [
            {
                "external_ref": "erp-1",
                "email": "A@x.ex",
                "first_name": "A",
                "last_name": "One",
                "phone": "0801",
                "applied_on": "2026-05-01",
            },
            {
                "external_ref": "erp-2",
                "email": "b@x.ex",
                "first_name": "B",
                "last_name": "Two",
                "applied_on": "2026-05-02T09:00:00+00:00",
            },
            {"external_ref": "erp-3", "email": "", "first_name": "No", "last_name": "Email"},
        ]
        with db.begin():
            db.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(tenant)})
            s1 = backfill(db, tenant_id=tenant, rows=rows)
        assert s1 == {"imported": 2, "skipped": 1, "total": 3}
        got = db.execute(
            text(
                "SELECT email, source, program, status, applied_on FROM applicants WHERE tenant_id = :t ORDER BY email"
            ),
            {"t": tenant},
        ).all()
        assert [g[0] for g in got] == ["a@x.ex", "b@x.ex"]
        assert all(g[1] == "erp_backfill" and g[2] == "Fiber Academy" and g[3] == "applied" for g in got)
        assert {str(g[4]) for g in got} == {"2026-05-01", "2026-05-02"}
        db.rollback()
        with db.begin():
            db.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(tenant)})
            s2 = backfill(db, tenant_id=tenant, rows=rows)
        assert s2["imported"] == 2
        n = db.execute(text("SELECT count(*) FROM applicants WHERE tenant_id = :t"), {"t": tenant}).scalar_one()
        assert n == 2


def test_reset_required_no_default_and_tenant_rls_refusals(genesis_database):
    admin, superuser, name = genesis_database
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    person = uuid.uuid4()
    with admin.begin() as conn:
        for tenant in (tenant_a, tenant_b):
            conn.execute(
                text("INSERT INTO tenants (id, slug, name) VALUES (:id, :s, 'T')"),
                {"id": tenant, "s": tenant.hex},
            )
        conn.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(tenant_a)})
        conn.execute(
            text("INSERT INTO parties (id, tenant_id, party_type, display_name) VALUES (:id, :t, 'person', 'P')"),
            {"id": person, "t": tenant_a},
        )
    with admin.connect() as conn, conn.begin():
        conn.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(tenant_a)})
        # Catalog: reset_required is NOT NULL with no default.
        nullable, default = conn.execute(
            text(
                "SELECT is_nullable, column_default FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'academy_credential_policies' "
                "AND column_name = 'reset_required'"
            )
        ).one()
        assert nullable == "NO" and default is None
        # Runtime: omitting it refuses with 23502 before any FK check.
        try:
            with conn.begin_nested():
                conn.execute(
                    text(
                        "INSERT INTO academy_credential_policies "
                        "(credential_id, tenant_id, email, failed_login_attempts, lifecycle_version) "
                        "VALUES (:c, :t, 'p@x.ex', 0, 1)"
                    ),
                    {"c": uuid.uuid4(), "t": tenant_a},
                )
            code = None
        except DBAPIError as exc:
            code = getattr(exc.orig, "sqlstate", None)
        assert code == "23502", code
    # Cross-tenant: tenant B session cannot see tenant A rows under RLS.
    from sqlalchemy.engine import make_url

    app_url = make_url(superuser).set(database=name, username="app_user", password=None)
    app_engine = create_engine(app_url.render_as_string(hide_password=False))
    try:
        with app_engine.connect() as conn, conn.begin():
            conn.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(tenant_b)})
            seen = conn.execute(text("SELECT count(*) FROM parties WHERE tenant_id = :t"), {"t": tenant_a}).scalar_one()
            assert seen == 0
            # Writing a row for tenant A under tenant B context is refused (42501).
            code = None
            try:
                with conn.begin_nested():
                    conn.execute(
                        text(
                            "INSERT INTO parties (id, tenant_id, party_type, display_name) "
                            "VALUES (:id, :t, 'person', 'X')"
                        ),
                        {"id": uuid.uuid4(), "t": tenant_a},
                    )
            except DBAPIError as exc:
                code = getattr(exc.orig, "sqlstate", None)
            assert code == "42501", code
    finally:
        app_engine.dispose()
