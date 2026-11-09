from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0004_assessment"; down_revision = "0003_cohorts"
branch_labels = None; depends_on = None
from sqlalchemy.dialects import postgresql


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
    op.create_table("question_banks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("course_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chapter_number", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        *_ts(),
        sa.UniqueConstraint("tenant_id", "id", name="uq_question_banks_tenant_id_id"))
    op.create_index("ix_question_banks_tenant_id", "question_banks", ["tenant_id"])
    op.create_index("ix_question_banks_course_id", "question_banks", ["course_id"])

    op.create_table("questions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("bank_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ext_id", sa.String(60), nullable=False),
        sa.Column("stem", sa.Text(), nullable=False),
        sa.Column("type", sa.String(12), nullable=False),
        sa.Column("options", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("correct", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("rubric_category", sa.String(12), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False, server_default=""),
        sa.Column("weight", sa.Integer(), nullable=False, server_default="1"),
        *_ts(),
        sa.UniqueConstraint("tenant_id", "bank_id", "ext_id", name="uq_questions_bank_ext"),
        sa.ForeignKeyConstraint(["tenant_id", "bank_id"],
                                ["question_banks.tenant_id", "question_banks.id"],
                                ondelete="CASCADE", name="fk_questions_tenant_bank"))
    for c in ("tenant_id", "bank_id"):
        op.create_index(f"ix_questions_{c}", "questions", [c])

    op.create_table("activities",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("course_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chapter_number", sa.Integer(), nullable=True),
        sa.Column("type", sa.String(16), nullable=False, server_default="mcq_test"),
        sa.Column("bank_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("pass_threshold", sa.Float(), nullable=False, server_default="0.0"),
        *_ts(),
        sa.UniqueConstraint("tenant_id", "id", name="uq_activities_tenant_id_id"))
    for c in ("tenant_id", "course_id", "bank_id"):
        op.create_index(f"ix_activities_{c}", "activities", [c])

    op.create_table("submissions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("activity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("person_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("answers", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("attempt_no", sa.Integer(), nullable=False, server_default="1"),
        *_ts(),
        sa.UniqueConstraint("tenant_id", "id", name="uq_submissions_tenant_id_id"),
        sa.ForeignKeyConstraint(["tenant_id", "activity_id"],
                                ["activities.tenant_id", "activities.id"],
                                ondelete="CASCADE", name="fk_submissions_tenant_activity"))
    for c in ("tenant_id", "activity_id", "person_id"):
        op.create_index(f"ix_submissions_{c}", "submissions", [c])

    op.create_table("scores",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("submission_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("max_score", sa.Float(), nullable=False),
        sa.Column("fraction", sa.Float(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("per_item", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("source", sa.String(10), nullable=False, server_default="auto"),
        sa.Column("override_reason", sa.Text(), nullable=True),
        *_ts(),
        sa.ForeignKeyConstraint(["tenant_id", "submission_id"],
                                ["submissions.tenant_id", "submissions.id"],
                                ondelete="CASCADE", name="fk_scores_tenant_submission"))
    for c in ("tenant_id", "submission_id"):
        op.create_index(f"ix_scores_{c}", "scores", [c])

    for t in ("question_banks", "questions", "activities", "submissions", "scores"):
        _rls(t)


def downgrade():
    op.drop_table("scores")
    op.drop_table("submissions")
    op.drop_table("activities")
    op.drop_table("questions")
    op.drop_table("question_banks")
