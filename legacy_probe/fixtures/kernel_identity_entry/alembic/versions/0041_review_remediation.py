"""review remediation: canonical tracks, durable mail, and concurrency guards.

Revision ID: 0041_review_remediation
Revises: 0040_merge_tracks_admissions
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0041_review_remediation"
down_revision = "0040_merge_tracks_admissions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_credentials",
        sa.Column("failed_login_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "user_credentials",
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
    )

    op.add_column(
        "applicants",
        sa.Column("track_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index("ix_applicants_track_id", "applicants", ["track_id"])
    op.create_foreign_key(
        "fk_applicants_tenant_cohort_track",
        "applicants",
        "cohort_tracks",
        ["tenant_id", "cohort_id", "track_id"],
        ["tenant_id", "cohort_id", "track_id"],
    )

    # Prefer an exact historical program→Track match. Where a cohort has only
    # one active track, that track is an unambiguous backfill.
    op.execute(
        """
        UPDATE applicants a
        SET track_id = t.id,
            program = t.name
        FROM cohort_tracks ct
        JOIN tracks t
          ON t.tenant_id = ct.tenant_id
         AND t.id = ct.track_id
        WHERE a.tenant_id = ct.tenant_id
          AND a.cohort_id = ct.cohort_id
          AND a.track_id IS NULL
          AND ct.status = 'active'
          AND t.status = 'active'
          AND lower(trim(a.program)) = lower(trim(t.name));
        """
    )
    op.execute(
        """
        WITH only_tracks AS (
            SELECT tenant_id, cohort_id, min(track_id::text)::uuid AS track_id
            FROM cohort_tracks
            WHERE status = 'active'
            GROUP BY tenant_id, cohort_id
            HAVING count(*) = 1
        )
        UPDATE applicants a
        SET track_id = ot.track_id,
            program = t.name
        FROM only_tracks ot
        JOIN tracks t
          ON t.tenant_id = ot.tenant_id
         AND t.id = ot.track_id
        WHERE a.tenant_id = ot.tenant_id
          AND a.cohort_id = ot.cohort_id
          AND a.track_id IS NULL;
        """
    )

    op.create_table(
        "email_outbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(60), nullable=False, server_default="generic"),
        sa.Column("recipient", sa.String(254), nullable=False),
        sa.Column("subject", sa.String(300), nullable=False),
        sa.Column("html_body", sa.Text(), nullable=False),
        sa.Column("text_body", sa.Text(), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "id", name="uq_email_outbox_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_email_outbox_idempotency"),
    )
    op.create_index("ix_email_outbox_tenant_id", "email_outbox", ["tenant_id"])
    op.create_index("ix_email_outbox_status", "email_outbox", ["status"])
    op.execute("ALTER TABLE email_outbox ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE email_outbox FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY email_outbox_tenant_isolation ON email_outbox
        USING (tenant_id = app_current_tenant_id())
        WITH CHECK (tenant_id = app_current_tenant_id())
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON email_outbox TO app_user, platform_api")

    # Repair historical duplicate numbering before the database starts enforcing
    # the canonical one-attempt-number contract.
    op.execute(
        """
        WITH numbered AS (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY tenant_id, activity_id, person_id
                       ORDER BY created_at, id
                   ) AS attempt_no
            FROM submissions
        )
        UPDATE submissions s
        SET attempt_no = n.attempt_no
        FROM numbered n
        WHERE s.id = n.id;
        """
    )
    op.create_unique_constraint(
        "uq_submissions_person_activity_attempt",
        "submissions",
        ["tenant_id", "activity_id", "person_id", "attempt_no"],
    )

    # Preserve the newest open randomized sitting and close stale duplicates.
    op.execute(
        """
        WITH ranked AS (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY tenant_id, activity_id, person_id
                       ORDER BY started_at DESC, id DESC
                   ) AS rn
            FROM activity_attempts
            WHERE submitted_at IS NULL
        )
        UPDATE activity_attempts a
        SET submitted_at = now()
        FROM ranked r
        WHERE a.id = r.id
          AND r.rn > 1;
        """
    )
    op.create_index(
        "uq_activity_attempts_open",
        "activity_attempts",
        ["tenant_id", "activity_id", "person_id"],
        unique=True,
        postgresql_where=sa.text("submitted_at IS NULL"),
    )

    # The audit ledger becomes the authoritative transition history from this
    # release onward; seed one honest baseline event for pre-existing rows.
    op.execute(
        """
        INSERT INTO audit_events (
            id, tenant_id, actor_person_id, action, entity_type, entity_id, details, created_at
        )
        SELECT gen_random_uuid(),
               a.tenant_id,
               NULL,
               'applicant.transition_baseline',
               'applicant',
               a.id::text,
               jsonb_build_object(
                   'from_status', NULL,
                   'to_status', a.status,
                   'reason', 'state before transition ledger cutover',
                   'source', 'migration'
               ),
               now()
        FROM applicants a;
        """
    )


def downgrade() -> None:
    op.drop_index("uq_activity_attempts_open", table_name="activity_attempts")
    op.drop_constraint(
        "uq_submissions_person_activity_attempt",
        "submissions",
        type_="unique",
    )
    op.drop_table("email_outbox")
    op.drop_constraint(
        "fk_applicants_tenant_cohort_track",
        "applicants",
        type_="foreignkey",
    )
    op.drop_index("ix_applicants_track_id", table_name="applicants")
    op.drop_column("applicants", "track_id")
    op.drop_column("user_credentials", "locked_until")
    op.drop_column("user_credentials", "failed_login_attempts")
