"""Academy lab product tables (templates, instances, operations).

Ported from the legacy Academy models at new-composition genesis;
worker-owned columns keep their server-default metadata so the web
tier's column-scoped INSERT grants (a0001) are never tripped.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from dotmac_kernel.models import Tenant
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    FetchedValue,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, TimestampMixin, uuid_pk


def _tenant_fk():
    return mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(Tenant.__table__.c.id, ondelete="CASCADE"),
        nullable=False,
        index=True,
    )


class LabTemplate(Base, TimestampMixin):
    __tablename__ = "lab_templates"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_lab_templates_tenant_id_id"),
        UniqueConstraint("activity_id", name="uq_lab_templates_activity_id"),
    )
    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    course_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)
    chapter_number: Mapped[int | None] = mapped_column(Integer)
    activity_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)
    slug: Mapped[str] = mapped_column(String(63), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    topology: Mapped[str] = mapped_column(Text, nullable=False, default="")
    instructions_html: Mapped[str] = mapped_column(Text, nullable=False, default="")
    instructions_md: Mapped[str | None] = mapped_column(Text)
    checks: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    seed_spec: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    limits: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # Per-node device access info shown to the instance owner (see
    # ``lab_content.parse_access``). Documented lab-device defaults only: never
    # populated from env, settings or secrets. Server default is written by
    # migration 0062 so rows that predate it read as "no access info".
    access: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))
    engine: Mapped[str] = mapped_column(String(20), nullable=False, default="containerlab")
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class LabInstance(Base, TimestampMixin):
    __tablename__ = "lab_instances"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_lab_instances_tenant_id_id"),
        # Global (not tenant-scoped) — containerlab's runtime namespace is
        # host-global, not tenant-scoped, so a tenant-scoped constraint would
        # not be sufficient to prevent a runtime name collision. Modeled as a
        # standalone unique ``Index`` (not ``UniqueConstraint``) because
        # that's the actual DB object `0057_lab_instance_name_unique.py`
        # creates via ``op.create_index(..., unique=True)`` — a UNIQUE
        # constraint would additionally create a `pg_constraint` row with no
        # backing migration for it, which `--autogenerate` would then flag as
        # drift on this table.
        Index("uq_lab_instances_instance_name", "instance_name", unique=True),
    )
    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    activity_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)
    person_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)
    instance_name: Mapped[str] = mapped_column(String(120), nullable=False)
    seed: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # These runtime fields are worker-owned.  Server-default metadata keeps a
    # normal app_user ORM insert from naming columns outside its column grant.
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'queued'"))
    consoles: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    error: Mapped[str | None] = mapped_column(Text, server_default=FetchedValue())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=FetchedValue())
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=FetchedValue())
    # Worker-owned, independent of `status` (see migration
    # 0058_lab_runtime_presence.py's module docstring): `status`
    # carries lifecycle/UI meaning and cannot simultaneously express physical
    # runtime existence. Exactly three values — "absent" (proven absent),
    # "present" (observed or successfully created), "unknown" (external
    # mutation began, result uncertain). Capacity accounting
    # (`_capacity_available` in `app/services/lab_operations.py`) counts
    # "present" and "unknown" together, regardless of lifecycle `status`.
    runtime_presence: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'absent'"))


class LabConsoleActivity(Base):
    """Web-tier observation that a learner used a console of this instance.

    One row per instance, written only by ``lab_console_activity.record`` under
    column-scoped grants (see migration 0063). It is a fact, not a decision:
    the idle reaper alone turns it into a keep-alive. ``lab_instances`` itself
    stays worker-owned.
    """

    __tablename__ = "lab_console_activity"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "instance_id"],
            ["lab_instances.tenant_id", "lab_instances.id"],
            name="fk_lab_console_activity_instance",
            ondelete="CASCADE",
        ),
    )
    instance_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class LabOperation(Base, TimestampMixin):
    """One requested deploy/destroy/check for a `LabInstance` — a work queue
    row, not a decision record.

    The lab worker consumes this queue. ``kind`` and ``state`` remain
    application-validated rather than DB CHECK constrained, and
    ``requested_by`` remains an attribution value rather than a declared FK.

    The load-bearing boundary is in the migration's grants, not here: the web
    tier may SELECT (observe) and INSERT only the request columns (`id`,
    `tenant_id`, `instance_id`, `kind`, `requested_by`) — every worker-owned
    column, including `state` and `attempts`, is excluded from that INSERT
    grant entirely, so the web tier can neither forge worker-owned state at
    creation nor UPDATE a row to claim, heartbeat, or finish it. Only
    `academy_lab_worker` may claim and settle a row; `app_admin` is reserved for
    migrations/offline maintenance. `uq_lab_operations_open_per_instance` backs
    the other half of the guarantee: at most one queued-or-claimed operation
    may exist per instance at a time.
    """

    __tablename__ = "lab_operations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "instance_id"],
            ["lab_instances.tenant_id", "lab_instances.id"],
            ondelete="CASCADE",
            name="fk_lab_operations_tenant_instance",
        ),
        Index(
            "uq_lab_operations_open_per_instance",
            "instance_id",
            unique=True,
            postgresql_where=text("state IN ('queued', 'claimed')"),
        ),
        Index("ix_lab_operations_claim", "state", "not_before", "requested_at"),
        CheckConstraint(
            "claim_lease_seconds IS NULL OR claim_lease_seconds > 0",
            name="ck_lab_operations_claim_lease_positive",
        ),
        CheckConstraint(
            "state <> 'claimed' OR claim_lease_seconds IS NOT NULL",
            name="ck_lab_operations_claim_lease_required",
        ),
        # Mirrors migration 0060_lab_conditional_ops.py's CHECK exactly —
        # see that migration's module docstring for why every branch below
        # uses explicit `IS NOT NULL`/`IS NULL` guards rather than a bare `=`
        # comparison: Postgres's three-valued CHECK logic treats a NULL
        # comparison result as "not FALSE" (i.e. satisfied), so a half-null
        # (origin, runtime_precondition) tuple could otherwise silently pass.
        CheckConstraint(
            "(origin IS NULL AND runtime_precondition IS NULL) "
            "OR (kind = 'deploy' AND origin IS NOT NULL AND origin = 'runtime_repair' "
            "AND runtime_precondition IS NOT NULL AND runtime_precondition = 'absent') "
            "OR (kind = 'destroy' AND origin IS NOT NULL AND origin = 'runtime_cleanup' "
            "AND runtime_precondition IS NOT NULL AND runtime_precondition = 'present')",
            name="ck_lab_operations_conditional_runtime",
        ),
    )
    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = _tenant_fk()
    instance_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # `server_default=` (not client-side `default=`) here: app_user's
    # column-level INSERT grant (see the a0001 grant matrix) does not cover this
    # column. A client-side `default=` makes SQLAlchemy mention the column
    # explicitly in the compiled INSERT even when the value equals the server
    # default — tripping the missing column grant. Declaring no default at all
    # is just as broken the other way: the ORM's unit-of-work flush still
    # sends every mapped column explicitly (as `NULL`) unless it is told a
    # server-side default exists, which fails the NOT NULL constraint outright.
    # `server_default=` is what makes the ORM omit the column from the INSERT
    # and refresh the object from the row Postgres actually inserted, exactly
    # like `requested_at`/`not_before` below.
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'queued'"))
    requested_by: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    not_before: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    # Same reasoning as `state` above applies to every worker-owned column
    # below, nullable or not: Postgres requires INSERT privilege on any
    # column *mentioned* in the statement regardless of whether the value
    # sent is NULL, and a mapped_column with no default at all is still
    # mentioned explicitly (as NULL) on every ORM flush. `FetchedValue()` is a
    # metadata-only server-generation marker — it emits no DDL default — and
    # tells SQLAlchemy's ORM to omit the column so the
    # column-level INSERT grant is never tripped by an otherwise-normal
    # insert that doesn't set it.
    # Worker-stamped duration; FetchedValue omits it from request-only INSERTs.
    claim_lease_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True, server_default=FetchedValue())
    claimed_by: Mapped[str | None] = mapped_column(String(200), server_default=FetchedValue())
    # Structural decomposition of `claimed_by` for restart-reclaim (migration
    # 0059_lab_claim_owner.py): `claimed_by` stays the sole fencing value
    # (byte-identical to `worker_identity()`'s composite string, compared
    # exactly everywhere a lease is checked); these two columns exist purely
    # so a freshly-started worker can find "rows I claimed in a PREVIOUS
    # incarnation on this same host" without parsing that composite string.
    # Same `FetchedValue()` reasoning as `claimed_by` above: metadata-only
    # server-generation marker, so app_user's column-scoped INSERT grant (see
    # migration 0055) never needs to cover these worker-owned columns either.
    claimed_host: Mapped[str | None] = mapped_column(String(255), server_default=FetchedValue())
    claimed_epoch: Mapped[str | None] = mapped_column(String(64), server_default=FetchedValue())
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=FetchedValue())
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=FetchedValue())
    # Same reasoning as `state` above.
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=FetchedValue())
    last_error: Mapped[str | None] = mapped_column(Text, server_default=FetchedValue())
    # Reconciler conditional operations (migration 0060_lab_conditional_ops.py):
    # worker-owned, like every column above. `origin` is audit/provenance
    # only ("runtime_repair" for a conditional deploy, "runtime_cleanup" for
    # a conditional destroy) and never INDEPENDENTLY determines whether a
    # precondition holds — only `runtime_precondition` ("absent"/"present")
    # does that. `origin` (with `kind`) IS compared in `run_claimed()`'s
    # `is_conditional_deploy`/`is_conditional_destroy`, but only to confirm
    # which of the two valid conditional shapes a row claims to be; a
    # malformed/mismatched shape is already rejected by the CHECK constraint
    # below before this code ever runs. Same `FetchedValue()` reasoning as
    # `claimed_host`/`claimed_epoch` above: metadata-only server-generation
    # marker, so app_user's column-scoped INSERT grant (see a0001_lab_domain)
    # never needs to cover these worker-owned columns either.
    origin: Mapped[str | None] = mapped_column(String(32), server_default=FetchedValue())
    runtime_precondition: Mapped[str | None] = mapped_column(String(16), server_default=FetchedValue())
