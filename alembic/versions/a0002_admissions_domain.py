"""Academy admissions-domain product tables (a0002 lineage).

Creates the product-owned admissions/onboarding/facilitator tables with
tenant RLS, mirroring the a0001 posture (FORCE RLS, tenant isolation via
``app_current_tenant_id()``, no PUBLIC grants). DDL is rendered from the
ported product models at genesis and frozen here so the migration stays
self-contained.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "a0002_admissions_domain"
down_revision = "a0001_lab_domain"
branch_labels = None
depends_on = None

_TABLES = (
    "applicants",
    "onboarding_tasks",
    "facilitator_nominations",
    "facilitator_requirements",
    "tenant_entrance_defaults",
)

_DDL = r"""
CREATE TABLE applicants (
	id UUID NOT NULL,
	tenant_id UUID NOT NULL,
	email VARCHAR(254) NOT NULL,
	first_name VARCHAR(80) NOT NULL,
	last_name VARCHAR(80) NOT NULL,
	phone VARCHAR(40),
	program VARCHAR(120),
	status VARCHAR(20) DEFAULT 'applied' NOT NULL,
	source VARCHAR(30) DEFAULT 'website' NOT NULL,
	external_ref VARCHAR(64),
	notes VARCHAR(1000),
	applied_on DATE DEFAULT CURRENT_DATE NOT NULL,
	person_id UUID,
	cohort_id UUID,
	track_id UUID,
	assessment_score FLOAT,
	assessment_level VARCHAR(20),
	assessment_profile JSONB,
	assessment_taken_at TIMESTAMP WITH TIME ZONE,
	assessment_bank_id UUID,
	assessment_bank_version_id UUID,
	assessment_return_url VARCHAR(2048),
	assessment_erp_synced_at TIMESTAMP WITH TIME ZONE,
	assessment_result_version INTEGER DEFAULT '0' NOT NULL,
	assessment_token_hash VARCHAR(128),
	assessment_started_at TIMESTAMP WITH TIME ZONE,
	assessment_elapsed_seconds INTEGER DEFAULT '0' NOT NULL,
	assessment_time_exceeded BOOLEAN DEFAULT 'false' NOT NULL,
	assessment_answers JSONB,
	assessment_valid BOOLEAN,
	assessment_invalid_reason VARCHAR(40),
	assessment_reset_count INTEGER DEFAULT '0' NOT NULL,
	assessment_deadline TIMESTAMP WITH TIME ZONE,
	invite_sent_at TIMESTAMP WITH TIME ZONE,
	onboarding_token_hash VARCHAR(128),
	date_of_birth DATE,
	state VARCHAR(60),
	city VARCHAR(60),
	highest_qualification VARCHAR(60),
	field_of_study VARCHAR(120),
	years_experience INTEGER,
	"current_role" VARCHAR(120),
	has_device BOOLEAN,
	has_internet BOOLEAN,
	can_work_at_height BOOLEAN,
	available_from DATE,
	heard_from VARCHAR(60),
	cv_url VARCHAR(500),
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_applicants_tenant_id_id UNIQUE (tenant_id, id),
	CONSTRAINT fk_applicants_tenant_cohort_track FOREIGN KEY(tenant_id, cohort_id, track_id) REFERENCES mod_learning.cohort_tracks (tenant_id, cohort_id, track_id),
	CONSTRAINT fk_applicants_tenant_assessment_bank FOREIGN KEY(tenant_id, assessment_bank_id) REFERENCES mod_learning.question_banks (tenant_id, id),
	CONSTRAINT fk_applicants_tenant_assessment_bank_version FOREIGN KEY(tenant_id, assessment_bank_version_id) REFERENCES mod_learning.question_bank_versions (tenant_id, id),
	FOREIGN KEY(tenant_id) REFERENCES tenants (id) ON DELETE CASCADE
);

CREATE INDEX ix_applicants_assessment_bank_id ON applicants (assessment_bank_id);

CREATE INDEX ix_applicants_assessment_bank_version_id ON applicants (assessment_bank_version_id);

CREATE INDEX ix_applicants_status ON applicants (tenant_id, status);

CREATE INDEX ix_applicants_tenant_id ON applicants (tenant_id);

CREATE INDEX ix_applicants_track_id ON applicants (track_id);

CREATE UNIQUE INDEX uq_applicants_tenant_external_ref ON applicants (tenant_id, external_ref) WHERE external_ref IS NOT NULL;

CREATE UNIQUE INDEX uq_applicants_tenant_local_email ON applicants (tenant_id, email) WHERE external_ref IS NULL;

CREATE TABLE onboarding_tasks (
	id UUID NOT NULL,
	tenant_id UUID NOT NULL,
	applicant_id UUID NOT NULL,
	key VARCHAR(40) NOT NULL,
	label VARCHAR(200) NOT NULL,
	order_index INTEGER NOT NULL,
	status VARCHAR(10) DEFAULT 'pending' NOT NULL,
	completed_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_onboarding_tasks_applicant_key UNIQUE (tenant_id, applicant_id, key),
	CONSTRAINT fk_onboarding_tasks_tenant_applicant FOREIGN KEY(tenant_id, applicant_id) REFERENCES applicants (tenant_id, id) ON DELETE CASCADE,
	FOREIGN KEY(tenant_id) REFERENCES tenants (id) ON DELETE CASCADE
);

CREATE INDEX ix_onboarding_tasks_applicant ON onboarding_tasks (tenant_id, applicant_id);

CREATE INDEX ix_onboarding_tasks_tenant_id ON onboarding_tasks (tenant_id);

CREATE TABLE facilitator_nominations (
	id UUID NOT NULL,
	tenant_id UUID NOT NULL,
	person_id UUID NOT NULL,
	nominated_by_person_id UUID,
	scope_type VARCHAR(20) NOT NULL,
	cohort_id UUID,
	offering_id UUID,
	status VARCHAR(20) NOT NULL,
	activated_at TIMESTAMP WITH TIME ZONE,
	revoked_at TIMESTAMP WITH TIME ZONE,
	activation_reason VARCHAR(40),
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_facilitator_nominations_tenant_id_id UNIQUE (tenant_id, id),
	CONSTRAINT fk_facilitator_nominations_person FOREIGN KEY(tenant_id, person_id) REFERENCES parties (tenant_id, id) ON DELETE CASCADE,
	CONSTRAINT fk_facilitator_nominations_person_subtype FOREIGN KEY(person_id) REFERENCES party_persons (party_id) ON DELETE CASCADE,
	CONSTRAINT fk_facilitator_nominations_nominator FOREIGN KEY(tenant_id, nominated_by_person_id) REFERENCES parties (tenant_id, id),
	CONSTRAINT fk_facilitator_nominations_nominator_subtype FOREIGN KEY(nominated_by_person_id) REFERENCES party_persons (party_id),
	CONSTRAINT fk_facilitator_nominations_cohort FOREIGN KEY(tenant_id, cohort_id) REFERENCES mod_learning.cohorts (tenant_id, id) ON DELETE CASCADE,
	CONSTRAINT fk_facilitator_nominations_offering FOREIGN KEY(tenant_id, offering_id) REFERENCES mod_learning.course_offerings (tenant_id, id) ON DELETE CASCADE,
	CONSTRAINT ck_facilitator_nominations_scope CHECK (scope_type IN ('course', 'cohort')),
	CONSTRAINT ck_facilitator_nominations_status CHECK (status IN ('pending', 'active', 'revoked')),
	CONSTRAINT ck_facilitator_nominations_target CHECK ((scope_type = 'cohort' AND cohort_id IS NOT NULL AND offering_id IS NULL) OR (scope_type = 'course' AND cohort_id IS NULL AND offering_id IS NOT NULL)),
	FOREIGN KEY(tenant_id) REFERENCES tenants (id) ON DELETE CASCADE
);

CREATE INDEX ix_facilitator_nominations_cohort_id ON facilitator_nominations (cohort_id);

CREATE INDEX ix_facilitator_nominations_offering_id ON facilitator_nominations (offering_id);

CREATE INDEX ix_facilitator_nominations_person_id ON facilitator_nominations (person_id);

CREATE INDEX ix_facilitator_nominations_status ON facilitator_nominations (status);

CREATE INDEX ix_facilitator_nominations_tenant_id ON facilitator_nominations (tenant_id);

CREATE UNIQUE INDEX uq_facilitator_nominations_cohort_scope ON facilitator_nominations (tenant_id, person_id, cohort_id) WHERE scope_type = 'cohort';

CREATE UNIQUE INDEX uq_facilitator_nominations_course_scope ON facilitator_nominations (tenant_id, person_id, offering_id) WHERE scope_type = 'course';

CREATE TABLE facilitator_requirements (
	id UUID NOT NULL,
	tenant_id UUID NOT NULL,
	nomination_id UUID NOT NULL,
	course_id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_facilitator_requirements_tenant_id_id UNIQUE (tenant_id, id),
	CONSTRAINT uq_facilitator_requirements_nomination_course UNIQUE (tenant_id, nomination_id, course_id),
	CONSTRAINT fk_facilitator_requirements_nomination FOREIGN KEY(tenant_id, nomination_id) REFERENCES facilitator_nominations (tenant_id, id) ON DELETE CASCADE,
	CONSTRAINT fk_facilitator_requirements_course FOREIGN KEY(tenant_id, course_id) REFERENCES mod_learning.courses (tenant_id, id),
	FOREIGN KEY(tenant_id) REFERENCES tenants (id) ON DELETE CASCADE
);

CREATE INDEX ix_facilitator_requirements_course_id ON facilitator_requirements (course_id);

CREATE INDEX ix_facilitator_requirements_nomination_id ON facilitator_requirements (nomination_id);

CREATE INDEX ix_facilitator_requirements_tenant_id ON facilitator_requirements (tenant_id);

CREATE TABLE tenant_entrance_defaults (
	tenant_id UUID NOT NULL,
	default_bank_id UUID,
	default_time_limit_minutes INTEGER,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (tenant_id),
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
