"""Create the shared v2 genesis database for the legacy-compat harness.

Usage: python -m legacy_probe.genesis <dbname>
Requires TEST_DATABASE_URL (superuser on the disposable PG). Prints
export lines for TEST_DATABASE_URL (app_user), TEST_MIGRATION_DATABASE_URL
(app_admin) and TEST_LAB_WORKER_DATABASE_URL for the created database.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    name = sys.argv[1]
    superuser = os.environ["TEST_DATABASE_URL"]
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
        conn.execute(text(f'CREATE DATABASE "{name}"'))
        conn.execute(
            text(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles "
                "WHERE rolname = 'academy_lab_worker') THEN "
                "CREATE ROLE academy_lab_worker LOGIN NOINHERIT BYPASSRLS NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION; END IF; END $$"
            )
        )
    setup = create_engine(make_url(superuser).set(database=name), isolation_level="AUTOCOMMIT")
    with setup.connect() as conn:
        conn.execute(text("ALTER SCHEMA public OWNER TO app_admin"))
        conn.execute(text(f'GRANT CREATE ON DATABASE "{name}" TO app_admin'))
        conn.execute(text(f'GRANT CONNECT ON DATABASE "{name}" TO app_user, platform_api, academy_lab_worker'))
    setup.dispose()
    server.dispose()

    def url_for(user: str) -> str:
        return make_url(superuser).set(database=name, username=user, password=None).render_as_string(
            hide_password=False
        )

    from alembic import command
    from alembic.config import Config

    os.environ["MIGRATION_DATABASE_URL"] = url_for("app_admin")
    os.environ["DATABASE_URL"] = url_for("app_user")
    command.upgrade(Config(str(ROOT / "alembic.ini")), "heads")
    admin = create_engine(url_for("app_admin"))
    with admin.begin() as conn:
        conn.execute(text((ROOT / "deploy/grants.sql").read_text()))
    admin.dispose()
    print(f"export TEST_DATABASE_URL={url_for('app_user')}")
    print(f"export TEST_MIGRATION_DATABASE_URL={url_for('app_admin')}")
    print(f"export TEST_LAB_WORKER_DATABASE_URL={url_for('academy_lab_worker')}")
    print(f"export DATABASE_URL={url_for('app_user')}")


if __name__ == "__main__":
    main()
