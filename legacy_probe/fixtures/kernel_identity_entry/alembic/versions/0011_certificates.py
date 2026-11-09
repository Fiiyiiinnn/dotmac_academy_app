"""Certificates: credential issued on course completion.

Slice 2d.
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0011_certificates"; down_revision = "0010_course_completions"
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
    op.create_table("certificates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("person_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("course_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("serial", sa.String(32), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        *_ts(),
        sa.UniqueConstraint("tenant_id", "id", name="uq_certificates_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "person_id", "course_id",
                            name="uq_certificates_person_course"),
        sa.UniqueConstraint("tenant_id", "serial", name="uq_certificates_tenant_serial"),
        sa.ForeignKeyConstraint(["tenant_id", "course_id"], ["courses.tenant_id", "courses.id"],
                                ondelete="CASCADE", name="fk_certificates_tenant_course"))
    for c in ("tenant_id", "person_id", "course_id"):
        op.create_index(f"ix_certificates_{c}", "certificates", [c])
    _rls("certificates")


def downgrade():
    op.drop_table("certificates")
