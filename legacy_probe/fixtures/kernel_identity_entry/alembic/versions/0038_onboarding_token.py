"""applicants.onboarding_token_hash — self-serve onboarding portal access link."""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0038_onboarding_token"
down_revision = "0037_pause_entrance_timer"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("applicants", sa.Column("onboarding_token_hash", sa.String(128), nullable=True))


def downgrade() -> None:
    op.drop_column("applicants", "onboarding_token_hash")
