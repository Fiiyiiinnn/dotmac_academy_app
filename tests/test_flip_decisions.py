"""Flip-decision proofs (Michael, 2026-11-07).

1. People imported without credentials stay as parties with no login
   (excluding them would orphan enrollments/applications/history) and
   can claim the account through the genuine invitation flow.
2. Failed email-outbox rows are not carried by the importer (pending
   is drained operationally before the snapshot).
3. The quiet-state guard refuses a snapshot while lab work is queued
   or claimed, instances are running, or email is still pending.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from starlette.requests import Request

from scripts.flip_preflight import quiet_state_reasons
from scripts.v6_import import import_identity, import_product_tables

_LEGACY_DDL = """
CREATE TABLE tenants (id uuid PRIMARY KEY, slug text, name text, is_active boolean,
                      created_at timestamptz, updated_at timestamptz, suspended_at timestamptz, deleted_at timestamptz);
CREATE TABLE tenant_domains (id uuid PRIMARY KEY, tenant_id uuid, domain text,
                             verified_at timestamptz, created_at timestamptz, updated_at timestamptz);
CREATE TABLE people (id uuid PRIMARY KEY, tenant_id uuid, email text, first_name text,
                     last_name text, created_at timestamptz, updated_at timestamptz,
                     status text, avatar_path text, prefs jsonb);
CREATE TABLE roles (id uuid PRIMARY KEY, tenant_id uuid, slug text, name text, created_at timestamptz, updated_at timestamptz);
CREATE TABLE person_roles (id uuid PRIMARY KEY, tenant_id uuid, person_id uuid, role_id uuid, created_at timestamptz, updated_at timestamptz);
CREATE TABLE user_credentials (id uuid PRIMARY KEY, tenant_id uuid, person_id uuid,
                     email text, password_hash text, created_at timestamptz,
                     updated_at timestamptz, failed_login_attempts int, locked_until timestamptz);
"""


def _scratch(superuser: str):
    server = create_engine(superuser, isolation_level="AUTOCOMMIT")
    name = f"academy_v2_flip_{uuid.uuid4().hex[:10]}"
    with server.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    server.dispose()
    url = make_url(superuser).set(database=name).render_as_string(hide_password=False)
    return name, create_engine(url)


def _drop(superuser: str, name: str) -> None:
    server = create_engine(superuser, isolation_level="AUTOCOMMIT")
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


def test_imported_person_without_login_claims_via_invite(genesis_database):
    from dotmac_kernel.models import Tenant, UserCredential
    from dotmac_kernel.security import verify_password

    from app.models.account_token import AccountToken
    from app.services.credential_authentication import sign_in
    from app.services.security import hash_token
    from app.web import lifecycle as routes

    admin, superuser, dest_name = genesis_database
    tenant_id, person_id = uuid.uuid4(), uuid.uuid4()
    src_name, src = _scratch(superuser)
    try:
        with src.begin() as conn:
            conn.execute(text(_LEGACY_DDL))
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, is_active, created_at, updated_at) VALUES (:id, 'flip', 'Flip', true, now(), now())"
                ),
                {"id": tenant_id},
            )
            conn.execute(
                text(
                    "INSERT INTO tenant_domains VALUES (:id, :t, 'flip.example', "
                    "'2026-01-02T03:04:05Z', '2026-01-02T03:04:05Z', '2026-01-02T03:04:05Z')"
                ),
                {"id": uuid.uuid4(), "t": tenant_id},
            )
            conn.execute(
                text(
                    "INSERT INTO people VALUES (:id, :t, 'nologin@flip.edu', 'No', 'Login', "
                    "now(), now(), 'active', NULL, '{}'::jsonb)"
                ),
                {"id": person_id, "t": tenant_id},
            )
        # Import identity only; the person has no credential row anywhere.
        exceptions: list = []
        with src.connect() as sconn, admin.connect() as dconn, dconn.begin():
            assert import_identity(sconn, dconn, exceptions) == 1
        assert exceptions == []
        with admin.connect() as conn:
            domain, verified = conn.execute(
                text("SELECT domain, verified_at FROM tenant_domains WHERE tenant_id = :t"), {"t": tenant_id}
            ).one()
        assert domain == "flip.example" and verified is not None, "verified custom domain must carry"

        user_url = (
            make_url(superuser)
            .set(database=dest_name, username="app_user", password=None)
            .render_as_string(hide_password=False)
        )
        user_engine = create_engine(user_url)
        try:
            with Session(user_engine) as db:
                db.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(tenant_id)})
                assert db.scalars(select(UserCredential).where(UserCredential.party_id == person_id)).first() is None, (
                    "imported party must start with no login"
                )
                tenant = db.get(Tenant, tenant_id)
                raw = uuid.uuid4().hex + uuid.uuid4().hex
                db.add(
                    AccountToken(
                        tenant_id=tenant_id,
                        person_id=person_id,
                        kind="invite",
                        token_hash=hash_token(raw),
                        expires_at=datetime.now(UTC) + timedelta(hours=1),
                    )
                )
                db.commit()
                db.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(tenant_id)})
                request = Request({"type": "http", "method": "POST", "path": "/accept-invite", "headers": []})
                request.state.tenant = tenant
                response = routes.accept_submit(request, token=raw, password="welcome12", db=db)
                assert response.status_code == 200
                assert "Account activated" in response.body.decode()
                credential = db.scalars(select(UserCredential).where(UserCredential.party_id == person_id)).one()
                assert verify_password("welcome12", credential.password_hash)
                db.commit()
                db.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(tenant_id)})
                result = sign_in(db, tenant_id=tenant_id, email="nologin@flip.edu", password="welcome12")
                assert result is not None and result.account.id == person_id
                db.rollback()
        finally:
            user_engine.dispose()
    finally:
        src.dispose()
        _drop(superuser, src_name)


def test_importer_does_not_carry_failed_outbox(genesis_database, monkeypatch):
    from scripts import v6_import

    _admin, superuser, _name = genesis_database
    src_name, src = _scratch(superuser)
    dst_name, dst = _scratch(superuser)
    monkeypatch.setattr(v6_import, "PRODUCT_TABLES", ("email_outbox",))
    from scripts import product_transfer

    monkeypatch.setitem(product_transfer.PRODUCT_COLUMNS, "email_outbox", ("id", "status", "payload"))
    ddl = "CREATE TABLE email_outbox (id uuid PRIMARY KEY, status text, payload jsonb)"
    try:
        with src.begin() as conn:
            conn.execute(text(ddl))
            for status in ("pending", "sent", "failed", "cancelled"):
                conn.execute(
                    text("INSERT INTO email_outbox VALUES (:id, :s, '{}'::jsonb)"),
                    {"id": uuid.uuid4(), "s": status},
                )
        with dst.begin() as conn:
            conn.execute(text(ddl))
        exceptions: list = []
        with src.connect() as sconn, dst.connect() as dconn, dconn.begin():
            totals = import_product_tables(sconn, dconn, exceptions)
        assert exceptions == []
        assert totals == {"email_outbox": 3}
        with dst.connect() as conn:
            statuses = [r[0] for r in conn.execute(text("SELECT status FROM email_outbox ORDER BY status"))]
        assert statuses == ["cancelled", "pending", "sent"]
    finally:
        src.dispose()
        dst.dispose()
        _drop(superuser, src_name)
        _drop(superuser, dst_name)


def test_flip_preflight_quiet_state_guard(genesis_database):
    _admin, superuser, _name = genesis_database
    name, engine = _scratch(superuser)
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE lab_operations (id uuid PRIMARY KEY, state text)"))
            conn.execute(text("CREATE TABLE lab_instances (id uuid PRIMARY KEY, status text, runtime_presence text)"))
            conn.execute(text("CREATE TABLE email_outbox (id uuid PRIMARY KEY, status text)"))
            conn.execute(text("CREATE TABLE lab_result_deliveries (id uuid PRIMARY KEY, status text)"))
            conn.execute(text("INSERT INTO lab_operations VALUES (:id, 'succeeded')"), {"id": uuid.uuid4()})
            conn.execute(text("INSERT INTO lab_instances VALUES (:id, 'reaped', 'absent')"), {"id": uuid.uuid4()})
            conn.execute(text("INSERT INTO email_outbox VALUES (:id, 'sent')"), {"id": uuid.uuid4()})
        with engine.connect() as conn:
            assert quiet_state_reasons(conn) == []
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO lab_operations VALUES (:id, 'claimed')"), {"id": uuid.uuid4()})
            conn.execute(text("INSERT INTO lab_instances VALUES (:id, 'active', 'absent')"), {"id": uuid.uuid4()})
            conn.execute(text("INSERT INTO email_outbox VALUES (:id, 'pending')"), {"id": uuid.uuid4()})
        with engine.connect() as conn:
            reasons = quiet_state_reasons(conn)
        assert any("lab_operations claimed=1" in r for r in reasons)
        assert any("lab_instances active=1" in r for r in reasons)
        assert any("email_outbox pending=1" in r for r in reasons)
    finally:
        engine.dispose()
        _drop(superuser, name)
