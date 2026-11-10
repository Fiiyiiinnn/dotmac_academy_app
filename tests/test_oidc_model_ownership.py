"""Hosted a0011 qualification: declared model, physical catalog and tenant RLS."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, insert, inspect, select, text, update
from sqlalchemy.exc import DBAPIError

from app.models.oidc_login import AcademyOidcLoginState
from tests.issue2_wave1_fixtures import (
    admin_engine,
    admin_session,
    app_user_session,
    tenant_a,
    tenant_b,
)

assert admin_engine and admin_session and app_user_session and tenant_a and tenant_b  # Imported pytest fixtures.

TABLE = AcademyOidcLoginState.__table__


def test_oidc_genesis_catalog_matches_declared_model_and_forces_rls(genesis_database):
    admin, _superuser, _name = genesis_database
    with admin.connect() as conn:
        row = conn.execute(
            text(
                "SELECT relrowsecurity, relforcerowsecurity, pg_get_userbyid(relowner) "
                "FROM pg_catalog.pg_class WHERE oid = 'public.academy_oidc_login_states'::regclass"
            )
        ).one()
        assert row == (True, True, "app_admin")
        policy = conn.execute(
            text(
                "SELECT policyname, cmd, qual, with_check FROM pg_catalog.pg_policies "
                "WHERE schemaname = 'public' AND tablename = 'academy_oidc_login_states'"
            )
        ).one()
        assert policy.policyname == "academy_oidc_login_states_tenant_isolation"
        assert policy.cmd == "ALL"
        assert policy.qual == policy.with_check
        assert "tenant_id = app_current_tenant_id()" in policy.qual

    inspector = inspect(admin)
    columns = {column["name"]: column for column in inspector.get_columns(TABLE.name, schema="public")}
    assert set(columns) == set(TABLE.c.keys())
    for column in TABLE.c:
        reflected = columns[column.name]
        assert reflected["nullable"] == column.nullable
        assert reflected["type"].compile(dialect=admin.dialect) == column.type.compile(dialect=admin.dialect)
    assert inspector.get_pk_constraint(TABLE.name, schema="public")["constrained_columns"] == ["id"]
    unique = inspector.get_unique_constraints(TABLE.name, schema="public")
    assert [(item["name"], item["column_names"]) for item in unique] == [
        ("uq_academy_oidc_login_tenant_state", ["tenant_id", "state_hash"])
    ]
    foreign_key = inspector.get_foreign_keys(TABLE.name, schema="public")
    assert len(foreign_key) == 1
    assert foreign_key[0]["constrained_columns"] == ["tenant_id"]
    assert foreign_key[0]["referred_table"] == "tenants"
    assert foreign_key[0]["referred_columns"] == ["id"]
    assert foreign_key[0]["options"]["ondelete"] == "CASCADE"
    assert any(
        item["name"] == "ix_academy_oidc_login_states_tenant_id" and item["column_names"] == ["tenant_id"]
        for item in inspector.get_indexes(TABLE.name, schema="public")
    )


def _state(tenant_id):
    now = datetime.now(UTC)
    return dict(
        id=uuid4(),
        tenant_id=tenant_id,
        state_hash="a" * 64,
        provider_binding="academy-keycloak",
        code_verifier="test-verifier",
        nonce="test-nonce",
        redirect_uri="https://academy.example/login/oidc/callback",
        return_to="/",
        issued_at=int(now.timestamp()),
        expires_at=now + timedelta(minutes=10),
        created_at=now,
    )


def test_oidc_table_enforces_tenant_scope_for_reads_and_writes(admin_session, app_user_session, tenant_a, tenant_b):
    states = [_state(tenant.id) for tenant in (tenant_a, tenant_b)]
    for state in states:
        admin_session.execute(
            text("SELECT set_config('app.current_tenant', :tenant, true)"), {"tenant": str(state["tenant_id"])}
        )
        admin_session.execute(insert(TABLE).values(**state))
    admin_session.commit()

    db = app_user_session
    assert db.scalar(text("SELECT current_user")) == "app_user"
    db.execute(text("SELECT set_config('app.current_tenant', '', true)"))
    assert db.execute(select(TABLE.c.id)).all() == []
    db.execute(text("SELECT set_config('app.current_tenant', :tenant, true)"), {"tenant": str(tenant_a.id)})
    assert db.scalars(select(TABLE.c.id)).all() == [states[0]["id"]]
    assert db.execute(update(TABLE).where(TABLE.c.id == states[1]["id"]).values(return_to="/changed")).rowcount == 0
    assert db.execute(delete(TABLE).where(TABLE.c.id == states[1]["id"])).rowcount == 0
    for statement in (
        insert(TABLE).values(**{**_state(tenant_b.id), "state_hash": "b" * 64}),
        update(TABLE).where(TABLE.c.id == states[0]["id"]).values(tenant_id=tenant_b.id),
    ):
        with pytest.raises(DBAPIError) as error, db.begin_nested():
            db.execute(statement)
        assert error.value.orig.sqlstate == "42501"
    assert db.execute(update(TABLE).where(TABLE.c.id == states[0]["id"]).values(return_to="/changed")).rowcount == 1
    assert db.execute(delete(TABLE).where(TABLE.c.id == states[0]["id"])).rowcount == 1
    db.execute(text("SELECT set_config('app.current_tenant', :tenant, true)"), {"tenant": str(tenant_b.id)})
    assert db.scalars(select(TABLE.c.id)).all() == [states[1]["id"]]
