"""Allow the lab worker to persist completion and certificate consequences.

Completion locks its own aggregate row; learner identity remains read-only.
The worker may update completion state, but cannot alter its identity keys or
ERP delivery projection. Intent delivery remains owned by its existing worker.
"""

from __future__ import annotations

from alembic import op

revision = "0061_lab_completion_grants"
down_revision = "0060_lab_conditional_ops"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT SELECT ON courses TO academy_lab_worker;")
    op.execute("GRANT SELECT ON course_completions TO academy_lab_worker;")
    op.execute(
        "GRANT INSERT (id, tenant_id, person_id, course_id, status, pct) "
        "ON course_completions TO academy_lab_worker;"
    )
    op.execute("GRANT SELECT, INSERT ON certificates TO academy_lab_worker;")
    op.execute(
        "GRANT UPDATE (status, pct, completed_at, updated_at) "
        "ON course_completions TO academy_lab_worker;"
    )


def downgrade() -> None:
    op.execute(
        "REVOKE UPDATE (status, pct, completed_at, updated_at) "
        "ON course_completions FROM academy_lab_worker;"
    )
    op.execute(
        "REVOKE INSERT (id, tenant_id, person_id, course_id, status, pct) "
        "ON course_completions FROM academy_lab_worker;"
    )
    op.execute("REVOKE SELECT ON course_completions FROM academy_lab_worker;")
    op.execute("REVOKE SELECT, INSERT ON certificates FROM academy_lab_worker;")
    op.execute("REVOKE SELECT ON courses FROM academy_lab_worker;")
