"""applicants: exam autosave/resume, validity gate, reset audit.

Three problems this fixes on a LIVE, timed, one-attempt, unproctored exam:

1. Answers were only persisted on final submit. A dropped connection lost every
   answer AND the clock kept running, so the applicant returned to "Time is up"
   with no score and no way to re-sit — a permanent lockout from a network blip.
   ``assessment_answers`` autosaves progress so a reconnect resumes intact.

2. No validity gate. A click-through guessing ~8/30 in three minutes was stored
   as a real "beginner" profile, polluting both the admissions ranking and the
   talent pool. ``assessment_valid`` / ``assessment_invalid_reason`` mark those
   as an ABSENCE of data rather than a weak candidate.

3. ``assessment_reset_count`` audits admin resets (the recovery path for #1).
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0034_entrance_resilience"
down_revision = "0033_tenant_default_entrance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "applicants",
        sa.Column("assessment_answers", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("applicants", sa.Column("assessment_valid", sa.Boolean(), nullable=True))
    op.add_column("applicants", sa.Column("assessment_invalid_reason", sa.String(length=40), nullable=True))
    op.add_column(
        "applicants",
        sa.Column("assessment_reset_count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("applicants", "assessment_reset_count")
    op.drop_column("applicants", "assessment_invalid_reason")
    op.drop_column("applicants", "assessment_valid")
    op.drop_column("applicants", "assessment_answers")
