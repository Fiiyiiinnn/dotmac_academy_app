"""Course offerings: explicit cohort<->course link + backfill from discipline.

Slice 1 of the LMS buildout. Replaces discipline-string matching as the
entitlement source. The backfill preserves existing access by linking every
cohort to each course sharing its discipline.
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0008_course_offerings"; down_revision = "0007_person_profile"
branch_labels = None; depends_on = None


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
    op.create_table("course_offerings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("cohort_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("course_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        *_ts(),
        sa.UniqueConstraint("tenant_id", "id", name="uq_course_offerings_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "cohort_id", "course_id",
                            name="uq_course_offerings_cohort_course"),
        sa.ForeignKeyConstraint(["tenant_id", "cohort_id"],
                                ["cohorts.tenant_id", "cohorts.id"],
                                ondelete="CASCADE", name="fk_course_offerings_tenant_cohort"),
        sa.ForeignKeyConstraint(["tenant_id", "course_id"],
                                ["courses.tenant_id", "courses.id"],
                                ondelete="CASCADE", name="fk_course_offerings_tenant_course"))
    for c in ("tenant_id", "cohort_id", "course_id"):
        op.create_index(f"ix_course_offerings_{c}", "course_offerings", [c])

    _rls("course_offerings")

    # Backfill: preserve current access by linking every cohort to each course
    # sharing its discipline (the access rule before this slice). Runs as the
    # migration role (RLS-bypassing); tenant scoping carried by the join.
    op.execute("""
        INSERT INTO course_offerings
            (id, tenant_id, cohort_id, course_id, status, created_at, updated_at)
        SELECT gen_random_uuid(), c.tenant_id, c.id, co.id, 'active', now(), now()
        FROM cohorts c
        JOIN courses co
          ON co.tenant_id = c.tenant_id AND co.discipline = c.discipline
        ON CONFLICT (tenant_id, cohort_id, course_id) DO NOTHING;
    """)


def downgrade():
    op.drop_table("course_offerings")
