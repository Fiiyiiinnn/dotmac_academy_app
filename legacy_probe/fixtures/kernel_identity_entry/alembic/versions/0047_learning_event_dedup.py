"""Partial unique index enforcing once-per-subject learning events.

Revision ID: 0047_learning_event_dedup
Revises: 0046_success_queue

The ledger's once-per-(person, kind, subject) invariant (chapter_completed,
certificate_earned) was enforced only by a SELECT-then-INSERT check in the
service, which two concurrent requests can both pass — double-counting a
completion or certificate. This adds the database guard so the invariant holds
under concurrency; the writer switches to INSERT ... ON CONFLICT DO NOTHING.
Day-throttled view kinds are intentionally excluded (their races cost at most
one extra view row per day and cannot be a static unique constraint).
"""

from __future__ import annotations

from alembic import op

revision = "0047_learning_event_dedup"
down_revision = "0046_success_queue"
branch_labels = None
depends_on = None

_INDEX = "uq_learning_events_once_per_subject"
_KINDS = "('chapter_completed', 'certificate_earned')"


def upgrade() -> None:
    op.execute(
        f"CREATE UNIQUE INDEX {_INDEX} ON learning_events "
        f"(tenant_id, person_id, kind, subject_id) WHERE kind IN {_KINDS}"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
