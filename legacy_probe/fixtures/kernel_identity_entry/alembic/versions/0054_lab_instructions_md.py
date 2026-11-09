"""Add `lab_templates.instructions_md` — the canonical Markdown source used to
render per-instance instructions (seed placeholders interpolated at request
time, not baked into stored HTML).

Rendered instructions must vary per learner (each `LabInstance.seed` differs),
so a single pre-rendered `instructions_html` per template can never carry that.
`instructions_md` is the stored canonical source projection the runtime
interpolates against; `instructions_html` is kept for backward compatibility
and as a fallback when a template predates this migration.

Backfilled from `instructions_html` on existing rows: it is valid Markdown
passthrough (a superset that markdown-in-markdown renders as literal text
around existing tags), and it preserves any literal `{{key}}` placeholders
that were already sitting unfilled in the stored HTML so they become
interpolatable immediately, without needing a re-import.

The column stays nullable — democode/demo template creation only supplies
`instructions_html`, and the read path already falls back to it when
`instructions_md IS NULL`.

Revision ID: 0054_lab_instructions_md
Revises: 0053_entrance_defaults
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# Kept to 24 chars deliberately: alembic_version.version_num is VARCHAR(32).
# The original id here ("0054_lab_template_instructions_md", 33 chars) exceeded
# that and failed CI with psycopg.errors.StringDataRightTruncation — caught by
# CI, not by review, since nothing short of actually running the migration
# checks this. Every other revision id in this directory stays well under 32.
revision = "0054_lab_instructions_md"
down_revision = "0053_entrance_defaults"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("lab_templates", sa.Column("instructions_md", sa.Text(), nullable=True))
    op.execute("UPDATE lab_templates SET instructions_md = instructions_html")


def downgrade() -> None:
    op.drop_column("lab_templates", "instructions_md")
