"""Initial multi-tenant schema with RLS.

Creates:
- DB roles `app_admin` (offline bypass), `platform_api` (online platform routes),
  and `app_user` (tenant routes, RLS-enforced)
- `tenants` and `tenant_domains` (NOT under RLS — platform-level)
- Tenant-scoped people, auth, RBAC, and audit tables with RLS policies
- Grants on platform_api and app_user

Run as a superuser the first time so the CREATE ROLE statements succeed; subsequent
upgrades can use app_admin.

Revision ID: 0001_initial_tenant_schema
Revises:
Create Date: 2026-05-04
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0001_initial_tenant_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    _ensure_roles()
    _create_tenants_table()
    _create_tenant_domains_table()
    _create_people_table()
    _create_auth_tables()
    _create_rbac_tables()
    _create_audit_events_table()
    _create_current_tenant_function()
    _apply_rls()
    _grant_roles()


def downgrade() -> None:
    op.drop_table("auth_sessions")
    op.drop_table("audit_events")
    op.drop_table("person_roles")
    op.drop_table("roles")
    op.drop_table("user_credentials")
    op.drop_table("people")
    op.drop_table("tenant_domains")
    op.drop_table("tenants")
    op.execute("DROP FUNCTION IF EXISTS app_current_tenant_id();")
    # Roles are NOT dropped on downgrade — other databases or future migrations may use them.


# ─────────────────────────────────────────────────────────────────────────────
# Roles
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_roles() -> None:
    """Create database roles if they don't exist.

    Idempotent. Passwords NOT set here — operators set them out of band.
    Connection strings in the env wire each role to its DATABASE_URL.
    """
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_admin') THEN
                CREATE ROLE app_admin LOGIN BYPASSRLS;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
                CREATE ROLE app_user LOGIN;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'platform_api') THEN
                CREATE ROLE platform_api LOGIN;
            END IF;
        END$$;
        """
    )


# ─────────────────────────────────────────────────────────────────────────────
# Platform tables (NOT under RLS)
# ─────────────────────────────────────────────────────────────────────────────

def _create_tenants_table() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("slug", sa.String(63), nullable=False, unique=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("suspended_at", sa.DateTime(timezone=True)),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_tenants_slug", "tenants", ["slug"])


def _create_tenant_domains_table() -> None:
    op.create_table(
        "tenant_domains",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("domain", sa.String(253), nullable=False, unique=True),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_tenant_domains_tenant_id", "tenant_domains", ["tenant_id"])


# ─────────────────────────────────────────────────────────────────────────────
# Tenant-scoped tables (RLS-protected)
# ─────────────────────────────────────────────────────────────────────────────

def _create_people_table() -> None:
    op.create_table(
        "people",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("first_name", sa.String(80), nullable=False),
        sa.Column("last_name", sa.String(80), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("tenant_id", "email", name="uq_people_tenant_email"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_people_tenant_id_id"),
    )
    op.create_index("ix_people_tenant_id", "people", ["tenant_id"])


def _create_auth_tables() -> None:
    op.create_table(
        "user_credentials",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "person_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "email",
            name="uq_user_credentials_tenant_email",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "person_id"],
            ["people.tenant_id", "people.id"],
            ondelete="CASCADE",
            name="fk_user_credentials_tenant_person",
        ),
    )
    op.create_index("ix_user_credentials_tenant_id", "user_credentials", ["tenant_id"])
    op.create_index("ix_user_credentials_person_id", "user_credentials", ["person_id"])

    op.create_table(
        "auth_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "person_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "token_hash",
            name="uq_auth_sessions_tenant_token_hash",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "person_id"],
            ["people.tenant_id", "people.id"],
            ondelete="CASCADE",
            name="fk_auth_sessions_tenant_person",
        ),
    )
    op.create_index("ix_auth_sessions_tenant_id", "auth_sessions", ["tenant_id"])
    op.create_index("ix_auth_sessions_person_id", "auth_sessions", ["person_id"])


def _create_rbac_tables() -> None:
    op.create_table(
        "roles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("slug", sa.String(63), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("tenant_id", "slug", name="uq_roles_tenant_slug"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_roles_tenant_id_id"),
    )
    op.create_index("ix_roles_tenant_id", "roles", ["tenant_id"])

    op.create_table(
        "person_roles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("person_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "person_id",
            "role_id",
            name="uq_person_roles_member",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "person_id"],
            ["people.tenant_id", "people.id"],
            ondelete="CASCADE",
            name="fk_person_roles_tenant_person",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "role_id"],
            ["roles.tenant_id", "roles.id"],
            ondelete="CASCADE",
            name="fk_person_roles_tenant_role",
        ),
    )
    op.create_index("ix_person_roles_tenant_id", "person_roles", ["tenant_id"])
    op.create_index("ix_person_roles_person_id", "person_roles", ["person_id"])
    op.create_index("ix_person_roles_role_id", "person_roles", ["role_id"])


def _create_audit_events_table() -> None:
    op.create_table(
        "audit_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("actor_person_id", postgresql.UUID(as_uuid=True)),
        sa.Column("action", sa.String(120), nullable=False),
        sa.Column("entity_type", sa.String(120), nullable=False),
        sa.Column("entity_id", sa.String(120)),
        sa.Column(
            "details",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_audit_events_tenant_id", "audit_events", ["tenant_id"])
    op.create_index("ix_audit_events_actor_person_id", "audit_events", ["actor_person_id"])


def _create_current_tenant_function() -> None:
    """Return the current tenant setting as uuid, or NULL when unset/invalid."""
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_current_tenant_id()
        RETURNS uuid
        LANGUAGE plpgsql
        STABLE
        AS $$
        BEGIN
            RETURN NULLIF(current_setting('app.current_tenant', true), '')::uuid;
        EXCEPTION
            WHEN invalid_text_representation THEN
                RETURN NULL;
        END;
        $$;
        """
    )


def _apply_rls() -> None:
    """Enable RLS on tenant-scoped tables."""
    for table in (
        "people",
        "user_credentials",
        "auth_sessions",
        "roles",
        "person_roles",
        "audit_events",
    ):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")
        op.execute(
            f"""
            CREATE POLICY {table}_tenant_isolation ON {table}
                USING (tenant_id = app_current_tenant_id())
                WITH CHECK (tenant_id = app_current_tenant_id());
            """
        )


def _grant_roles() -> None:
    """Grant explicit online privileges; app_admin remains for migrations/offline ops."""
    op.execute("GRANT USAGE ON SCHEMA public TO app_user, platform_api;")
    op.execute("GRANT EXECUTE ON FUNCTION app_current_tenant_id() TO app_user, platform_api;")

    op.execute("GRANT SELECT ON tenants, tenant_domains TO app_user, platform_api;")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON "
        "people, user_credentials, auth_sessions, roles, person_roles TO app_user;"
    )
    op.execute("GRANT SELECT, INSERT ON audit_events TO app_user;")
    op.execute("GRANT INSERT, UPDATE, DELETE ON tenants, tenant_domains TO platform_api;")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON "
        "people, user_credentials, auth_sessions, roles, person_roles TO platform_api;"
    )
    op.execute("GRANT SELECT, INSERT ON audit_events TO platform_api;")
