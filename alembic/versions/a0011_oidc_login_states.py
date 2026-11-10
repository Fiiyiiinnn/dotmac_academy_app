"""OIDC login ceremony state table (a0011).

Server-side, single-use ceremony rows for the Keycloak staff login
(docs/oidc-login.md). The state value itself is stored hashed; the
PKCE verifier and nonce never leave this table. Forced tenant RLS
mirrors a0003. DDL is frozen here; the model lives in
app/models/oidc_login.py.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "a0011_oidc_login_states"
down_revision = "a0010_audit_source_application"
branch_labels = None
depends_on = None

_TABLE = "academy_oidc_login_states"

_DDL = r"""
CREATE TABLE public.academy_oidc_login_states (
	id UUID NOT NULL,
	tenant_id UUID NOT NULL,
	state_hash VARCHAR(64) NOT NULL,
	provider_binding VARCHAR(80) NOT NULL,
	code_verifier TEXT NOT NULL,
	nonce TEXT NOT NULL,
	redirect_uri TEXT NOT NULL,
	return_to TEXT NOT NULL,
	issued_at BIGINT NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_academy_oidc_login_tenant_state UNIQUE (tenant_id, state_hash),
	CONSTRAINT fk_academy_oidc_login_tenant FOREIGN KEY(tenant_id) REFERENCES tenants (id) ON DELETE CASCADE
);

CREATE INDEX ix_academy_oidc_login_states_tenant_id ON public.academy_oidc_login_states (tenant_id);
"""


def upgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(sa.text("SELECT current_user")) != "app_admin":
        raise ValueError("Academy assembly migration requires app_admin")
    for statement in _DDL.split(";\n"):
        if statement.strip():
            op.execute(statement)
    op.execute(f"ALTER TABLE public.{_TABLE} OWNER TO app_admin")
    op.execute(f"ALTER TABLE public.{_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE public.{_TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {_TABLE}_tenant_isolation ON public.{_TABLE} "
        "USING (tenant_id = public.app_current_tenant_id()) "
        "WITH CHECK (tenant_id = public.app_current_tenant_id())"
    )
    op.execute(f"REVOKE ALL ON TABLE public.{_TABLE} FROM PUBLIC")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON public.{_TABLE} TO app_user")


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS public.{_TABLE} CASCADE")
