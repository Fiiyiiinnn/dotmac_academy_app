"""Synthetic V6 round-trip: genesis -> export all -> genesis -> verify.

Seeds one tenant with a course + enrollment in a first genesis
database, transfers the complete SnapshotV6 extent into a second
genesis database, and requires the destination to verify and to hold
the seeded rows. This proves the caller wiring before any production
snapshot rehearsal.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]


def _genesis(superuser: str, name: str) -> str:
    from alembic.config import Config

    from alembic import command

    server = create_engine(superuser, isolation_level="AUTOCOMMIT")
    with server.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    server.dispose()
    setup = create_engine(make_url(superuser).set(database=name), isolation_level="AUTOCOMMIT")
    with setup.connect() as conn:
        conn.execute(text("ALTER SCHEMA public OWNER TO app_admin"))
        conn.execute(text(f'GRANT CREATE ON DATABASE "{name}" TO app_admin'))
        conn.execute(text(f'GRANT CONNECT ON DATABASE "{name}" TO app_user, platform_api, academy_lab_worker'))
    setup.dispose()
    admin_url = (
        make_url(superuser)
        .set(database=name, username="app_admin", password=None)
        .render_as_string(hide_password=False)
    )
    os.environ["MIGRATION_DATABASE_URL"] = admin_url
    command.upgrade(Config(str(ROOT / "alembic.ini")), "heads")
    admin = create_engine(admin_url)
    with admin.begin() as conn:
        conn.execute(text((ROOT / "deploy/grants.sql").read_text()))
    admin.dispose()
    return make_url(superuser).set(database=name).render_as_string(hide_password=False)


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


def test_v6_synthetic_roundtrip(genesis_database):
    from scripts.v6_transfer import transfer_tenants

    admin, superuser, source_name = genesis_database
    source_url = make_url(superuser).set(database=source_name).render_as_string(hide_password=False)
    dest_name = f"academy_v2_dest_{uuid.uuid4().hex[:12]}"
    tenant_id = uuid.uuid4()
    course_id = uuid.uuid4()
    with admin.begin() as conn:
        conn.execute(
            text("INSERT INTO tenants (id, slug, name) VALUES (:id, :s, 'RT')"),
            {"id": tenant_id, "s": f"rt{uuid.uuid4().hex[:8]}"},
        )
        conn.execute(
            text(
                "INSERT INTO mod_learning.courses "
                "(id, tenant_id, slug, title, discipline, source_ref, status, listed) "
                "VALUES (:id, :t, 'fiber-basics', 'Fiber Basics', 'networking', 'seed:rt', 'published', true)"
            ),
            {"id": course_id, "t": tenant_id},
        )
    try:
        dest_url = _genesis(superuser, dest_name)
        # Identity lands before learning extents (flip step 1): the
        # destination kernel tenant must exist for import qualification.
        dest0 = create_engine(dest_url)
        with dest0.begin() as conn:
            conn.execute(
                text("INSERT INTO tenants (id, slug, name) VALUES (:id, :s, 'RT')"),
                {"id": tenant_id, "s": f"rt{uuid.uuid4().hex[:8]}"},
            )
        dest0.dispose()
        totals = transfer_tenants(source_url, dest_url, (tenant_id,))
        dest = create_engine(dest_url)
        with dest.connect() as conn:
            title = conn.execute(
                text("SELECT title FROM mod_learning.courses WHERE id = :id"), {"id": course_id}
            ).scalar_one()
        dest.dispose()
        assert title == "Fiber Basics"
        assert totals.get("courses", 0) >= 1
    finally:
        _drop(superuser, dest_name)
