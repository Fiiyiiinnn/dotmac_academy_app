"""Platform-wide settings key/value store.

Creates ``platform_settings`` (NO tenant_id, NO RLS — like ``tenants``) so admins
can configure SMTP / email toggles / branding / lab limits in the browser without
editing ``.env``. DB values override env defaults; absent keys fall back to env.

Grants mirror the platform tables: app_user + platform_api may SELECT; only
platform_api may write (app_admin owns the table from this migration).

Revision ID: 0006_platform_settings
Revises: 0005_labs
Create Date: 2026-06-27
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0006_platform_settings"
down_revision = "0005_labs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "platform_settings",
        sa.Column("key", sa.String(80), primary_key=True),
        sa.Column("value", sa.Text(), nullable=True),
    )
    op.execute("GRANT SELECT ON platform_settings TO app_user, platform_api;")
    op.execute("GRANT INSERT, UPDATE, DELETE ON platform_settings TO platform_api;")


def downgrade() -> None:
    op.drop_table("platform_settings")
