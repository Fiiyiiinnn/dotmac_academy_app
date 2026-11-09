"""Academy delivery product tables (a0005 lineage).

Creates `email_outbox`, `reminder_log` and `reminder_preferences` with
forced tenant RLS mirroring a0001-a0004. DDL rendered from the ported
product models at genesis and frozen here.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "a0005_delivery_domain"
down_revision = "a0004_account_tokens"
branch_labels = None
depends_on = None

_TABLES = ("email_outbox", "reminder_log", "reminder_preferences")

_DDL = r"""
CREATE TABLE email_outbox (
	id UUID NOT NULL,
	tenant_id UUID NOT NULL,
	idempotency_key VARCHAR(200) NOT NULL,
	kind VARCHAR(60) DEFAULT 'generic' NOT NULL,
	recipient VARCHAR(254) NOT NULL,
	subject VARCHAR(300) NOT NULL,
	html_body TEXT NOT NULL,
	text_body TEXT,
	payload JSONB NOT NULL,
	status VARCHAR(20) DEFAULT 'pending' NOT NULL,
	attempts INTEGER DEFAULT '0' NOT NULL,
	available_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	sent_at TIMESTAMP WITH TIME ZONE,
	last_error VARCHAR(120),
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_email_outbox_tenant_id_id UNIQUE (tenant_id, id),
	CONSTRAINT uq_email_outbox_idempotency UNIQUE (tenant_id, idempotency_key),
	FOREIGN KEY(tenant_id) REFERENCES tenants (id) ON DELETE CASCADE
);

CREATE INDEX ix_email_outbox_status ON email_outbox (status);

CREATE INDEX ix_email_outbox_tenant_id ON email_outbox (tenant_id);

CREATE TABLE reminder_log (
	id UUID NOT NULL,
	tenant_id UUID NOT NULL,
	person_id UUID NOT NULL,
	event_kind VARCHAR(30) NOT NULL,
	occurrence_key VARCHAR(160) NOT NULL,
	title VARCHAR(240) NOT NULL,
	link VARCHAR(300),
	channel VARCHAR(20) NOT NULL,
	status VARCHAR(20) NOT NULL,
	outbox_key VARCHAR(200),
	sent_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_reminder_log_occurrence UNIQUE (tenant_id, person_id, occurrence_key),
	CONSTRAINT fk_reminder_log_tenant_person FOREIGN KEY(tenant_id, person_id) REFERENCES parties (tenant_id, id) ON DELETE CASCADE,
	CONSTRAINT fk_reminder_log_person FOREIGN KEY(person_id) REFERENCES party_persons (party_id) ON DELETE CASCADE,
	FOREIGN KEY(tenant_id) REFERENCES tenants (id) ON DELETE CASCADE
);

CREATE INDEX ix_reminder_log_event_kind ON reminder_log (event_kind);

CREATE INDEX ix_reminder_log_person_id ON reminder_log (person_id);

CREATE INDEX ix_reminder_log_status ON reminder_log (status);

CREATE INDEX ix_reminder_log_tenant_id ON reminder_log (tenant_id);

CREATE TABLE reminder_preferences (
	id UUID NOT NULL,
	tenant_id UUID NOT NULL,
	person_id UUID NOT NULL,
	frequency VARCHAR(20) NOT NULL,
	quiet_start_hour INTEGER,
	quiet_end_hour INTEGER,
	optouts JSONB NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_reminder_preferences_person UNIQUE (tenant_id, person_id),
	CONSTRAINT fk_reminder_preferences_tenant_person FOREIGN KEY(tenant_id, person_id) REFERENCES parties (tenant_id, id) ON DELETE CASCADE,
	CONSTRAINT fk_reminder_preferences_person FOREIGN KEY(person_id) REFERENCES party_persons (party_id) ON DELETE CASCADE,
	FOREIGN KEY(tenant_id) REFERENCES tenants (id) ON DELETE CASCADE
);

CREATE INDEX ix_reminder_preferences_person_id ON reminder_preferences (person_id);

CREATE INDEX ix_reminder_preferences_tenant_id ON reminder_preferences (tenant_id);
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
