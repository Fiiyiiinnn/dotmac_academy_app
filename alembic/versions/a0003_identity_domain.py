"""Academy identity-extension + notification product tables (a0003).

Creates the product-owned account profile / credential policy tables
(assembly-owned policy over kernel Party/credentials; no credential or
session material) and notifications, with forced tenant RLS mirroring
a0001/a0002. DDL is rendered from the ported product models at genesis
and frozen here.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "a0003_identity_domain"
down_revision = "a0002_admissions_domain"
branch_labels = None
depends_on = None

_TABLES = ("academy_account_profiles", "academy_credential_policies", "notifications")

_DDL = r"""
CREATE TABLE public.academy_account_profiles (
	party_id UUID NOT NULL,
	tenant_id UUID NOT NULL,
	status VARCHAR(20) NOT NULL,
	avatar_path VARCHAR(255),
	prefs JSONB NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (party_id),
	CONSTRAINT fk_academy_account_tenant_party FOREIGN KEY(tenant_id, party_id) REFERENCES public.parties (tenant_id, id) ON DELETE CASCADE,
	CONSTRAINT fk_academy_account_person FOREIGN KEY(party_id) REFERENCES public.party_persons (party_id) ON DELETE CASCADE
);

CREATE TABLE public.academy_credential_policies (
	credential_id UUID NOT NULL,
	tenant_id UUID NOT NULL,
	email VARCHAR(254) NOT NULL,
	failed_login_attempts INTEGER NOT NULL,
	lifecycle_version BIGINT NOT NULL,
	reset_required BOOLEAN NOT NULL,
	locked_until TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (credential_id),
	CONSTRAINT fk_academy_policy_tenant_credential FOREIGN KEY(tenant_id, credential_id) REFERENCES public.user_credentials (tenant_id, id) ON DELETE CASCADE,
	CONSTRAINT uq_academy_policy_tenant_email UNIQUE (tenant_id, email),
	CONSTRAINT ck_academy_policy_lifecycle_version CHECK (lifecycle_version >= 0)
);

CREATE TABLE notifications (
	id UUID NOT NULL,
	tenant_id UUID NOT NULL,
	person_id UUID NOT NULL,
	kind VARCHAR(40) NOT NULL,
	title VARCHAR(200) NOT NULL,
	body TEXT DEFAULT '' NOT NULL,
	link VARCHAR(255),
	read_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_notifications_tenant_id_id UNIQUE (tenant_id, id),
	CONSTRAINT fk_notifications_tenant_person FOREIGN KEY(tenant_id, person_id) REFERENCES parties (tenant_id, id) ON DELETE CASCADE,
	CONSTRAINT fk_notifications_person FOREIGN KEY(person_id) REFERENCES party_persons (party_id) ON DELETE CASCADE,
	FOREIGN KEY(tenant_id) REFERENCES tenants (id) ON DELETE CASCADE
);

CREATE INDEX ix_notifications_person_id ON notifications (person_id);

CREATE INDEX ix_notifications_tenant_id ON notifications (tenant_id);
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
