"""Assembly-owned policy; no credential or session material lives here.

Canonical table copies below are metadata FK references only, never DDL targets.
The assembly migration creates only the two Academy tables. This source capsule
does not register models or activate runtime readers.
"""

# Ported at new-composition genesis (identity/facilitator slice).

from datetime import datetime
from typing import cast
from uuid import UUID

import sqlalchemy as sa
from dotmac_kernel.models import Party, PartyPerson, Tenant, UserCredential
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class IdentityExtensionBase(DeclarativeBase):
    pass


for _model in (Tenant, Party, PartyPerson, UserCredential):
    cast(sa.Table, _model.__table__).to_metadata(IdentityExtensionBase.metadata, schema="public")


class AcademyAccountProfile(IdentityExtensionBase):
    __tablename__ = "academy_account_profiles"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["tenant_id", "party_id"],
            ["public.parties.tenant_id", "public.parties.id"],
            name="fk_academy_account_tenant_party",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["party_id"],
            ["public.party_persons.party_id"],
            name="fk_academy_account_person",
            ondelete="CASCADE",
        ),
        {"schema": "public"},
    )
    party_id: Mapped[UUID] = mapped_column(sa.Uuid, primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(sa.Uuid, nullable=False)
    status: Mapped[str] = mapped_column(sa.String(20), nullable=False)
    avatar_path: Mapped[str | None] = mapped_column(sa.String(255))
    prefs: Mapped[object] = mapped_column(JSONB(none_as_null=False), nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class AcademyCredentialPolicy(IdentityExtensionBase):
    __tablename__ = "academy_credential_policies"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["tenant_id", "credential_id"],
            ["public.user_credentials.tenant_id", "public.user_credentials.id"],
            name="fk_academy_policy_tenant_credential",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("tenant_id", "email", name="uq_academy_policy_tenant_email"),
        sa.CheckConstraint("lifecycle_version >= 0", name="ck_academy_policy_lifecycle_version"),
        {"schema": "public"},
    )
    credential_id: Mapped[UUID] = mapped_column(sa.Uuid, primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(sa.Uuid, nullable=False)
    email: Mapped[str] = mapped_column(sa.String(254), nullable=False)
    failed_login_attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    # Current adoption/CAS bookkeeping; never a historical password-change count.
    lifecycle_version: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    # Explicit current recovery restriction; never inferred from legacy proof.
    reset_required: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
