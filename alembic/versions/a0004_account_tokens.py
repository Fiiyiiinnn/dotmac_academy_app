"""Academy account-token product table (a0004 lineage).

Creates `account_tokens` (hashed one-time account tokens) with forced
tenant RLS mirroring a0001-a0003. DDL rendered from the ported product
model at genesis and frozen here.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "a0004_account_tokens"
down_revision = "a0003_identity_domain"
branch_labels = None
depends_on = None

_TABLES = ("account_tokens",)

_DDL = r"""
CREATE TABLE public.account_tokens (
	id UUID NOT NULL,
	tenant_id UUID NOT NULL,
	person_id UUID NOT NULL,
	kind VARCHAR(20) NOT NULL,
	token_hash VARCHAR(64) NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	used_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_account_tokens_tenant_id_id UNIQUE (tenant_id, id),
	CONSTRAINT uq_account_tokens_tenant_token_hash UNIQUE (tenant_id, token_hash),
	CONSTRAINT fk_account_tokens_tenant_person FOREIGN KEY(tenant_id, person_id) REFERENCES public.parties (tenant_id, id) ON DELETE CASCADE,
	CONSTRAINT fk_account_tokens_person FOREIGN KEY(person_id) REFERENCES public.party_persons (party_id) ON DELETE CASCADE,
	FOREIGN KEY(tenant_id) REFERENCES public.tenants (id) ON DELETE CASCADE
);

CREATE INDEX ix_account_tokens_token_hash ON public.account_tokens (token_hash);

CREATE INDEX ix_public_account_tokens_person_id ON public.account_tokens (person_id);

CREATE INDEX ix_public_account_tokens_tenant_id ON public.account_tokens (tenant_id);
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
