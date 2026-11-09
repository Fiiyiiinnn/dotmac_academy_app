"""Account suspension: people.status.

Slice 3c. Existing rows default to 'active'.
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0015_person_status"; down_revision = "0014_account_tokens"
branch_labels = None; depends_on = None


def upgrade():
    op.add_column("people", sa.Column("status", sa.String(20), nullable=False,
                                      server_default="active"))


def downgrade():
    op.drop_column("people", "status")
