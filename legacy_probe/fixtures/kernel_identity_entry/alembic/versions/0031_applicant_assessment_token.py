"""applicants.assessment_token_hash — self-serve entrance-exam access link."""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0031_applicant_assessment_token"
down_revision = "0030_entrance_assessment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("applicants", sa.Column("assessment_token_hash", sa.String(128), nullable=True))


def downgrade() -> None:
    op.drop_column("applicants", "assessment_token_hash")
