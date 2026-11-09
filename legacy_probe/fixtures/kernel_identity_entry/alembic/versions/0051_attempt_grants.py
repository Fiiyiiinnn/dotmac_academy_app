"""attempt_grants: per-learner extra attempts on one activity.

An administrator had no way to give a single learner another go at a graded
assessment. The only levers were raising ``activities.max_attempts``, which
re-opens it for everyone enrolled, or deleting the learner's submissions, which
buys the retake by destroying the record of the attempts that justified it.

A grant is additive and append-only: the effective limit is
``max_attempts + SUM(extra_attempts)`` for that (person, activity), and each row
keeps the reason and the granting admin as the audit trail for what is, on a
final, an exam decision.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0051_attempt_grants"
down_revision = "0050_enrollment_access_extension"
branch_labels = None
depends_on = None


def _ts(): return [
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())]


def _rls(table):
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")
    op.execute(f"CREATE POLICY {table}_tenant_isolation ON {table} "
               f"USING (tenant_id = app_current_tenant_id()) "
               f"WITH CHECK (tenant_id = app_current_tenant_id());")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO app_user, platform_api;")


def upgrade():
    op.create_table("attempt_grants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("activity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("person_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("extra_attempts", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("granted_by", postgresql.UUID(as_uuid=True), nullable=True),
        *_ts(),
        sa.UniqueConstraint("tenant_id", "id", name="uq_attempt_grants_tenant_id_id"),
        sa.ForeignKeyConstraint(["tenant_id", "activity_id"],
                                ["activities.tenant_id", "activities.id"],
                                ondelete="CASCADE", name="fk_attempt_grants_tenant_activity"))
    for c in ("tenant_id", "activity_id", "person_id"):
        op.create_index(f"ix_attempt_grants_{c}", "attempt_grants", [c])
    # The hot path: "how many extra attempts does this learner have here?"
    op.create_index("ix_attempt_grants_lookup", "attempt_grants",
                    ["tenant_id", "activity_id", "person_id"])
    _rls("attempt_grants")


def downgrade():
    op.drop_table("attempt_grants")
