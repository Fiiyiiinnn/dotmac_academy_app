"""Academy ERP completion-sync product tables (a0006 lineage).

Creates the ERP completion checkpoint + imported-key tables with forced
tenant RLS mirroring a0001-a0005. DDL rendered from the ported product
model at genesis and frozen here.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "a0006_erp_domain"
down_revision = "a0005_delivery_domain"
branch_labels = None
depends_on = None

_TABLES = ("academy_erp_completion_checkpoints", "academy_erp_imported_completion_keys")

_DDL = r"""
CREATE TABLE academy_erp_completion_checkpoints (
	tenant_id UUID NOT NULL,
	completion_id UUID NOT NULL,
	erp_synced_at TIMESTAMP WITH TIME ZONE,
	erp_synced_pct FLOAT,
	PRIMARY KEY (tenant_id, completion_id),
	CONSTRAINT fk_erp_checkpoint_tenant_completion FOREIGN KEY(tenant_id, completion_id) REFERENCES mod_learning.course_completions (tenant_id, id) ON DELETE CASCADE,
	FOREIGN KEY(tenant_id) REFERENCES tenants (id) ON DELETE CASCADE
);

CREATE TABLE academy_erp_imported_completion_keys (
	tenant_id UUID NOT NULL,
	completion_id UUID NOT NULL,
	PRIMARY KEY (tenant_id, completion_id),
	CONSTRAINT fk_erp_imported_key_tenant_completion FOREIGN KEY(tenant_id, completion_id) REFERENCES mod_learning.course_completions (tenant_id, id) ON DELETE CASCADE,
	FOREIGN KEY(tenant_id) REFERENCES tenants (id) ON DELETE CASCADE
);
"""


def upgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(sa.text("SELECT current_user")) != "app_admin":
        raise ValueError("Academy assembly migration requires app_admin")
    for statement in _DDL.split(";\n"):
        if statement.strip():
            op.execute(statement)
    for table in _TABLES:
        op.execute(f"ALTER TABLE public.{table} OWNER TO app_admin")
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_tenant_isolation ON public.{table} "
            "USING (tenant_id = public.app_current_tenant_id()) "
            "WITH CHECK (tenant_id = public.app_current_tenant_id())"
        )
        op.execute(f"REVOKE ALL ON TABLE public.{table} FROM PUBLIC")
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON public.{table} TO app_user")


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.execute(f"DROP TABLE IF EXISTS public.{table} CASCADE")
