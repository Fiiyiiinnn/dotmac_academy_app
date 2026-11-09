"""Account lifecycle tokens (password reset, invite, email verification).

Slice 3b.
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0014_account_tokens"; down_revision = "0013_course_prerequisites"
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
    op.create_table("account_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("person_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        *_ts(),
        sa.UniqueConstraint("tenant_id", "id", name="uq_account_tokens_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "token_hash", name="uq_account_tokens_tenant_token_hash"),
        sa.ForeignKeyConstraint(["tenant_id", "person_id"],
                                ["people.tenant_id", "people.id"],
                                ondelete="CASCADE", name="fk_account_tokens_tenant_person"))
    for c in ("tenant_id", "person_id", "token_hash"):
        op.create_index(f"ix_account_tokens_{c}", "account_tokens", [c])
    _rls("account_tokens")


def downgrade():
    op.drop_table("account_tokens")
