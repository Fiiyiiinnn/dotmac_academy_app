"""Add `lab_templates.access` — per-node device login info for the lab owner.

An optional list (see ``app.services.lab_content.parse_access``) of documented,
disposable lab-device defaults: node, protocol, username, default password and
a short note. It is imported from ``lab.yaml`` only, never from env or secrets,
and is shown only to the owner of a live instance of the lab.

Additive and online-safe: a NOT NULL column with a constant server default
(``'[]'::jsonb``) is a metadata-only change on PostgreSQL 11+, and existing
rows read as "no access info". Neither the web role nor the lab worker needs a
new grant (both already hold table-level access to ``lab_templates``).

Revision ID: 0062_lab_template_access
Revises: 0061_lab_completion_grants
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0062_lab_template_access"
down_revision = "0061_lab_completion_grants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "lab_templates",
        sa.Column(
            "access",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("lab_templates", "access")
