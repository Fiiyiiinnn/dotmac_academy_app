"""course_completions.erp_synced_at — track training pushes to dotmac_erp HR."""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0029_completion_erp_synced"; down_revision = "0028_onboarding_tasks"
branch_labels = None; depends_on = None


def upgrade():
    op.add_column(
        "course_completions",
        sa.Column("erp_synced_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade():
    op.drop_column("course_completions", "erp_synced_at")
