"""Admissions — prospective students applying to the academy.

An ``Applicant`` moves through a status pipeline before becoming an enrolled
learner (``Person`` + ``Enrollment``). It is the student equivalent of the ERP
ATS: applications previously landed in the ERP job-applicant table (the
"Fiber Academy" opening); this module lets them land in the academy directly.

Tenant-scoped and RLS-isolated like every other table.
"""

# Ported at new-composition genesis (a0002 lineage); table shapes unchanged.

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from dotmac_kernel.models import Tenant
from dotmac_learning.curriculum_models import QuestionBank, QuestionBankVersion
from dotmac_learning.participation_models import CohortTrack
from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, TimestampMixin, uuid_pk

# Pipeline: applied -> screened -> accepted -> onboarding -> enrolled,
# with rejected / waitlisted as off-ramps. Allowed transitions are enforced
# in app/services/admissions.py (no Postgres enum — repo convention is a
# String column validated in the service layer).
APPLICANT_STATUSES = (
    "applied",
    "screened",
    "accepted",
    "onboarding",
    "enrolled",
    "rejected",
    "waitlisted",
)


class Applicant(Base, TimestampMixin):
    __tablename__ = "applicants"
    __table_args__ = (
        # Public/local intake remains one row per email. ERP-originated rows are
        # one row per external application so the same person may apply for two
        # jobs without sharing a sitting or result between them.
        Index(
            "uq_applicants_tenant_local_email",
            "tenant_id",
            "email",
            unique=True,
            postgresql_where=text("external_ref IS NULL"),
        ),
        Index(
            "uq_applicants_tenant_external_ref",
            "tenant_id",
            "external_ref",
            unique=True,
            postgresql_where=text("external_ref IS NOT NULL"),
        ),
        Index("ix_applicants_status", "tenant_id", "status"),
        # Parallels the other tables' (tenant_id, id) unique so future children
        # can reference an applicant via a tenant-consistent composite FK.
        UniqueConstraint("tenant_id", "id", name="uq_applicants_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "cohort_id", "track_id"],
            [CohortTrack.__table__.c.tenant_id, CohortTrack.__table__.c.cohort_id, CohortTrack.__table__.c.track_id],
            name="fk_applicants_tenant_cohort_track",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "assessment_bank_id"],
            [QuestionBank.__table__.c.tenant_id, QuestionBank.__table__.c.id],
            name="fk_applicants_tenant_assessment_bank",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "assessment_bank_version_id"],
            [QuestionBankVersion.__table__.c.tenant_id, QuestionBankVersion.__table__.c.id],
            name="fk_applicants_tenant_assessment_bank_version",
        ),
    )

    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(Tenant.__table__.c.id, ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    email: Mapped[str] = mapped_column(String(254), nullable=False)
    first_name: Mapped[str] = mapped_column(String(80), nullable=False)
    last_name: Mapped[str] = mapped_column(String(80), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(40), nullable=True)

    # Display snapshot of the canonical Track name at application time.
    program: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # applied|screened|accepted|onboarding|enrolled|rejected|waitlisted
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="applied")
    # website|erp_backfill — provenance of the application.
    source: Mapped[str] = mapped_column(String(30), nullable=False, server_default="website")
    # ERP JobApplicant id when imported by the backfill (idempotency/provenance).
    external_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Free-text screening / classification notes.
    notes: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    # The date the application was made (may predate row creation on backfill).
    applied_on: Mapped[date] = mapped_column(Date, nullable=False, server_default=func.current_date())

    # Set when the applicant is converted to an enrolled learner (P2).
    person_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)

    # Canonical cohort/track placement selected at intake.
    cohort_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    track_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True, index=True)

    # Entrance-assessment result — a competency profile, stored once taken:
    #   assessment_score   overall fraction 0..1
    #   assessment_level   band (see entrance_exam.LEVELS)
    #   assessment_profile per-category fractions, e.g. {"numeracy": 0.8, "safety": 0.9}
    assessment_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    assessment_level: Mapped[str | None] = mapped_column(String(20), nullable=True)
    assessment_profile: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    assessment_taken_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # A per-application bank snapshot/override. Null retains the historical
    # cohort -> tenant fallback for ordinary Academy intake.
    assessment_bank_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True, index=True)
    # Immutable bank-version pin stamped at explicit sitting start (never at
    # grade time): the sitting is served and graded from exactly this
    # version even if a newer one publishes mid-sitting. Null = legacy
    # sitting started before versioning (served best-effort, see
    # entrance_exam.effective_bank_version_id).
    assessment_bank_version_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True, index=True)
    # Validated browser destination supplied by the ERP. It is navigational
    # only; authoritative result delivery is the signed server webhook.
    assessment_return_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    # Durable state-derived webhook queue marker and monotonic result revision.
    assessment_erp_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    assessment_result_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # HMAC of the self-serve entrance-exam access token (the raw is emailed once).
    assessment_token_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Timed sitting: server-stamped when explicitly started. All duration and
    # expiry decisions derive from this server timestamp, never browser input.
    assessment_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Server-derived elapsed snapshot retained for reporting and migration
    # compatibility. It is never accepted from a client.
    assessment_elapsed_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    assessment_time_exceeded: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    # Autosaved progress {ext_id: [chosen option, ...]}. Persisted as the candidate
    # answers, so a dropped connection resumes intact instead of losing the sitting.
    assessment_answers: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Validity gate. False = the sitting carries NO SIGNAL (near-chance score, or
    # submitted too fast to have engaged) — an absence of data, not a weak
    # candidate. Invalid sittings are excluded from score-ranked admissions.
    assessment_valid: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    assessment_invalid_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # Audit: how many times an admin reset this sitting (the recovery path for a
    # dropped connection / interrupted exam).
    assessment_reset_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    # Deadline model: the exam link stays valid until this moment, so a candidate
    # can pick their own good-connectivity time rather than being pinned to a slot.
    assessment_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When the tokenised invitation was queued in the transactional outbox. The
    # outbox row owns actual delivery status and retry evidence.
    invite_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # HMAC of the self-serve onboarding-portal access token (raw emailed once).
    onboarding_token_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # --- the evaluable application profile -------------------------------
    # Name/email/phone alone cannot be assessed. These are what an admissions
    # decision actually rests on, alongside the entrance-exam profile.
    date_of_birth: Mapped[date | None] = mapped_column(Date, nullable=True)
    state: Mapped[str | None] = mapped_column(String(60), nullable=True)
    city: Mapped[str | None] = mapped_column(String(60), nullable=True)
    highest_qualification: Mapped[str | None] = mapped_column(String(60), nullable=True)
    field_of_study: Mapped[str | None] = mapped_column(String(120), nullable=True)
    years_experience: Mapped[int | None] = mapped_column(Integer, nullable=True)
    current_role: Mapped[str | None] = mapped_column(String(120), nullable=True)
    has_device: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    has_internet: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    can_work_at_height: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    available_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    heard_from: Mapped[str | None] = mapped_column(String(60), nullable=True)
    cv_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Fields an admissions decision cannot sensibly be made without.
    REQUIRED_PROFILE = (
        "date_of_birth",
        "state",
        "city",
        "highest_qualification",
        "years_experience",
        "has_device",
        "has_internet",
    )

    @property
    def profile_complete(self) -> bool:
        """True when every field needed to evaluate this candidate is present."""
        return all(getattr(self, f) is not None for f in self.REQUIRED_PROFILE)

    @property
    def missing_profile_fields(self) -> list[str]:
        return [f for f in self.REQUIRED_PROFILE if getattr(self, f) is None]
