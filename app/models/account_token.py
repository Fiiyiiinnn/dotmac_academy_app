# Ported at new-composition genesis (accounts-core slice).
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import TimestampMixin, uuid_pk
from app.models.identity_extensions import IdentityExtensionBase


class AccountToken(IdentityExtensionBase, TimestampMixin):
    """Single-use, hashed token for account lifecycle flows.

    ``kind`` is an open vocabulary, including ``ical_feed``. Only the
    HMAC of the raw token is stored; the raw value is delivered to the user once.
    A token is valid while ``used_at`` is null and ``expires_at`` is in the future.
    """

    __tablename__ = "account_tokens"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_account_tokens_tenant_id_id"),
        UniqueConstraint("tenant_id", "token_hash", name="uq_account_tokens_tenant_token_hash"),
        Index("ix_account_tokens_token_hash", "token_hash"),
        ForeignKeyConstraint(
            ["tenant_id", "person_id"],
            ["public.parties.tenant_id", "public.parties.id"],
            ondelete="CASCADE",
            name="fk_account_tokens_tenant_person",
        ),
        ForeignKeyConstraint(
            ["person_id"], ["public.party_persons.party_id"], ondelete="CASCADE", name="fk_account_tokens_person"
        ),
        {"schema": "public"},
    )
    id: Mapped[UUID] = uuid_pk()
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("public.tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    person_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
