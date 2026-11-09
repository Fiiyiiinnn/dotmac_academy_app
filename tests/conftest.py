"""Shared hosted-PostgreSQL genesis fixture for the v2 composition."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]

# The product assembly constructs its (lazy) database runtime at import;
# give settings a parseable URL in test processes. Real genesis
# databases are per-test via the fixture below.
if os.getenv("TEST_DATABASE_URL"):
    os.environ.setdefault("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    os.environ.setdefault("PLATFORM_DATABASE_URL", os.environ["TEST_DATABASE_URL"])

# Bind the product runtime before any test module imports
# dotmac_kernel.db (whose reference runtime would otherwise claim this
# process's slot and make the required product bind refuse). The bind
# also installs the audit-action registry human-operation services
# validate against. Only when a test database is configured.
if os.getenv("TEST_DATABASE_URL"):
    from app.database_runtime import bind_academy_database_runtime

    bind_academy_database_runtime()


def _superuser_url() -> str:
    url = os.getenv("TEST_MIGRATION_DATABASE_URL") or os.getenv("TEST_DATABASE_URL")
    if not url:
        if os.getenv("CI") == "true":
            pytest.fail("CI requires TEST_MIGRATION_DATABASE_URL or TEST_DATABASE_URL")
        pytest.skip("TEST_DATABASE_URL not set — lab definer needs Postgres")
    return url


def _url_for(base: str, dbname: str, user: str | None = None) -> str:
    from sqlalchemy.engine import make_url

    url = make_url(base).set(database=dbname)
    if user is not None:
        url = url.set(username=user, password=None)
    return url.render_as_string(hide_password=False)


@pytest.fixture
def genesis_database():
    from alembic.config import Config

    from alembic import command

    superuser = _superuser_url()
    name = f"academy_v2_lab_{uuid.uuid4().hex[:12]}"
    server = create_engine(superuser, isolation_level="AUTOCOMMIT")
    with server.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
        conn.execute(
            text(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles "
                "WHERE rolname = 'academy_lab_worker') THEN "
                "CREATE ROLE academy_lab_worker LOGIN; END IF; END $$"
            )
        )
    admin_url = _url_for(superuser, name, user="app_admin")
    try:
        setup = create_engine(_url_for(superuser, name), isolation_level="AUTOCOMMIT")
        with setup.connect() as conn:
            conn.execute(text("ALTER SCHEMA public OWNER TO app_admin"))
            conn.execute(text(f'GRANT CREATE ON DATABASE "{name}" TO app_admin'))
            conn.execute(text(f'GRANT CONNECT ON DATABASE "{name}" TO app_user, platform_api, academy_lab_worker'))
        setup.dispose()
        cfg = Config(str(ROOT / "alembic.ini"))
        os.environ["MIGRATION_DATABASE_URL"] = admin_url
        command.upgrade(cfg, "heads")
        admin = create_engine(admin_url)
        with admin.begin() as conn:
            conn.execute(text((ROOT / "deploy/grants.sql").read_text()))
        yield admin, superuser, name
        admin.dispose()
    finally:
        with server.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :name AND pid <> pg_backend_pid()"
                ),
                {"name": name},
            )
            conn.execute(text(f'DROP DATABASE "{name}"'))
        server.dispose()


def _scoped(conn, tenant) -> None:
    conn.execute(
        text("SELECT set_config('app.current_tenant', :tenant, true)"),
        {"tenant": str(tenant)},
    )


def seed_lab_scenario(admin):
    """Tenant/person/course/lab activity/template/active instance as admin."""
    tenant, person = uuid.uuid4(), uuid.uuid4()
    course, activity = uuid.uuid4(), uuid.uuid4()
    template, instance = uuid.uuid4(), uuid.uuid4()
    with admin.begin() as conn:
        _scoped(conn, tenant)
        conn.execute(
            text("INSERT INTO public.tenants (id, slug, name) VALUES (:id, :slug, 'Academy')"),
            {"id": tenant, "slug": tenant.hex},
        )
        conn.execute(
            text(
                "INSERT INTO public.parties (id, tenant_id, party_type, "
                "display_name) VALUES (:id, :tenant, 'person', 'Learner')"
            ),
            {"id": person, "tenant": tenant},
        )
        conn.execute(
            text(
                "INSERT INTO public.party_persons (party_id, first_name, last_name) VALUES (:id, 'Learning', 'Person')"
            ),
            {"id": person},
        )
        conn.execute(
            text(
                "INSERT INTO mod_learning.courses (id, tenant_id, slug, "
                "title, discipline, source_ref, status, listed) VALUES "
                "(:id, :tenant, :slug, 'Course', 'engineering', 'source', "
                "'published', false)"
            ),
            {"id": course, "tenant": tenant, "slug": course.hex},
        )
        conn.execute(
            text(
                "INSERT INTO mod_learning.activities (id, tenant_id, "
                "course_id, title, type, assessment_mode, grading, "
                "pass_threshold, weight) VALUES (:id, :tenant, :course, "
                "'Lab', 'lab', 'graded', 'auto', 0.7, 1.0)"
            ),
            {"id": activity, "tenant": tenant, "course": course},
        )
        conn.execute(
            text(
                "INSERT INTO public.lab_templates (id, tenant_id, course_id, "
                "activity_id, slug, title, topology, checks) VALUES (:id, "
                ":tenant, :course, :activity, 'lab-1', 'Template', '', "
                '\'[{"id": "c1", "label": "Check 1", "weight": 1, '
                '"type": "command", "node": "n1", '
                '"command": "true"}]\'::jsonb)'
            ),
            {
                "id": template,
                "tenant": tenant,
                "course": course,
                "activity": activity,
            },
        )
        conn.execute(
            text(
                "INSERT INTO public.lab_instances (id, tenant_id, "
                "activity_id, person_id, instance_name, seed, status) "
                "VALUES (:id, :tenant, :activity, :person, :name, "
                "'{}'::jsonb, 'active')"
            ),
            {
                "id": instance,
                "tenant": tenant,
                "activity": activity,
                "person": person,
                "name": f"lab-{instance.hex[:12]}",
            },
        )
    return tenant, person, course, activity, template, instance
