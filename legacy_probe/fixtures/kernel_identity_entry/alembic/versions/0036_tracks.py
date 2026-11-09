"""tracks: cohort curriculum paths above course offerings.

Adds first-class tracks without replacing CourseOffering. Existing production
access remains intact because CourseOffering stays the entitlement source; this
migration backfills one legacy track per cohort from current offerings.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0036_tracks"
down_revision = "0035_applicant_profile"
branch_labels = None
depends_on = None


def _ts():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    ]


def _rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")
    op.execute(
        f"CREATE POLICY {table}_tenant_isolation ON {table} "
        f"USING (tenant_id = app_current_tenant_id()) "
        f"WITH CHECK (tenant_id = app_current_tenant_id());"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO app_user, platform_api;")


def upgrade() -> None:
    op.create_table(
        "tracks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("slug", sa.String(80), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        *_ts(),
        sa.UniqueConstraint("tenant_id", "slug", name="uq_tracks_tenant_slug"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_tracks_tenant_id_id"),
    )
    op.create_index("ix_tracks_tenant_id", "tracks", ["tenant_id"])
    _rls("tracks")

    op.create_table(
        "cohort_tracks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("cohort_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("track_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        *_ts(),
        sa.UniqueConstraint("tenant_id", "id", name="uq_cohort_tracks_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "cohort_id", "track_id", name="uq_cohort_tracks_cohort_track"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "cohort_id"],
            ["cohorts.tenant_id", "cohorts.id"],
            ondelete="CASCADE",
            name="fk_cohort_tracks_tenant_cohort",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "track_id"],
            ["tracks.tenant_id", "tracks.id"],
            ondelete="CASCADE",
            name="fk_cohort_tracks_tenant_track",
        ),
    )
    op.create_index("ix_cohort_tracks_tenant_id", "cohort_tracks", ["tenant_id"])
    op.create_index("ix_cohort_tracks_cohort_id", "cohort_tracks", ["cohort_id"])
    op.create_index("ix_cohort_tracks_track_id", "cohort_tracks", ["track_id"])
    _rls("cohort_tracks")

    op.create_table(
        "track_courses",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("track_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("course_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False, server_default="0"),
        *_ts(),
        sa.UniqueConstraint("tenant_id", "track_id", "course_id", name="uq_track_courses_track_course"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "track_id"],
            ["tracks.tenant_id", "tracks.id"],
            ondelete="CASCADE",
            name="fk_track_courses_tenant_track",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "course_id"],
            ["courses.tenant_id", "courses.id"],
            ondelete="CASCADE",
            name="fk_track_courses_tenant_course",
        ),
    )
    op.create_index("ix_track_courses_tenant_id", "track_courses", ["tenant_id"])
    op.create_index("ix_track_courses_track_id", "track_courses", ["track_id"])
    op.create_index("ix_track_courses_course_id", "track_courses", ["course_id"])
    _rls("track_courses")

    op.add_column("enrollments", sa.Column("track_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_index("ix_enrollments_track_id", "enrollments", ["track_id"])

    op.execute("""
        INSERT INTO tracks (id, tenant_id, slug, name, status, created_at, updated_at)
        SELECT gen_random_uuid(), c.tenant_id, 'cohort-' || c.id::text, c.name || ' Track', 'active', now(), now()
        FROM cohorts c
        ON CONFLICT (tenant_id, slug) DO NOTHING;
    """)
    op.execute("""
        INSERT INTO cohort_tracks (id, tenant_id, cohort_id, track_id, status, created_at, updated_at)
        SELECT gen_random_uuid(), c.tenant_id, c.id, t.id, 'active', now(), now()
        FROM cohorts c
        JOIN tracks t ON t.tenant_id = c.tenant_id AND t.slug = 'cohort-' || c.id::text
        ON CONFLICT (tenant_id, cohort_id, track_id) DO NOTHING;
    """)
    op.execute("""
        INSERT INTO track_courses (id, tenant_id, track_id, course_id, order_index, created_at, updated_at)
        SELECT gen_random_uuid(), co.tenant_id, ct.track_id, co.course_id,
               row_number() OVER (PARTITION BY co.tenant_id, co.cohort_id ORDER BY c.title, c.slug)::int,
               now(), now()
        FROM course_offerings co
        JOIN cohort_tracks ct
          ON ct.tenant_id = co.tenant_id AND ct.cohort_id = co.cohort_id AND ct.status = 'active'
        JOIN tracks t
          ON t.tenant_id = ct.tenant_id AND t.id = ct.track_id AND t.slug = 'cohort-' || co.cohort_id::text
        JOIN courses c
          ON c.tenant_id = co.tenant_id AND c.id = co.course_id
        WHERE co.status = 'active'
        ON CONFLICT (tenant_id, track_id, course_id) DO NOTHING;
    """)
    op.execute("""
        UPDATE enrollments e
        SET track_id = ct.track_id
        FROM cohort_tracks ct
        JOIN tracks t ON t.tenant_id = ct.tenant_id AND t.id = ct.track_id
        WHERE e.tenant_id = ct.tenant_id
          AND e.cohort_id = ct.cohort_id
          AND e.track_id IS NULL
          AND e.role_in_cohort = 'student'
          AND t.slug = 'cohort-' || e.cohort_id::text;
    """)

    op.create_foreign_key(
        "fk_enrollments_tenant_cohort_track",
        "enrollments",
        "cohort_tracks",
        ["tenant_id", "cohort_id", "track_id"],
        ["tenant_id", "cohort_id", "track_id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_enrollments_tenant_cohort_track", "enrollments", type_="foreignkey")
    op.drop_index("ix_enrollments_track_id", table_name="enrollments")
    op.drop_column("enrollments", "track_id")
    op.drop_table("track_courses")
    op.drop_table("cohort_tracks")
    op.drop_table("tracks")
