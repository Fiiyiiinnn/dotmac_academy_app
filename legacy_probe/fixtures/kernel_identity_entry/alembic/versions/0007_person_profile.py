"""Person profile fields: avatar_path + prefs.

Adds two columns to ``people``:
- ``avatar_path`` (String 255, nullable) — path to the person's avatar image.
- ``prefs`` (JSONB, NOT NULL, default ``{}``) — per-person preferences
  (e.g. notification opt-outs).

Revision ID: 0007_person_profile
Revises: 0006_platform_settings
Create Date: 2026-06-27
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0007_person_profile"
down_revision = "0006_platform_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("people", sa.Column("avatar_path", sa.String(255), nullable=True))
    op.add_column(
        "people",
        sa.Column("prefs", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
    )


def downgrade() -> None:
    op.drop_column("people", "prefs")
    op.drop_column("people", "avatar_path")
