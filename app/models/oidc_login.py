"""In-flight OIDC login ceremonies (server-side, single-use).

The row is the Academy half of `dotmac_auth_oidc`'s required StateStore:
the PKCE verifier and nonce live here, never in the browser; the `state`
value on the wire is only a lookup key and is stored hashed. Rows are
consumed by one atomic DELETE ... RETURNING in
`app.services.oidc_login`, so a ceremony completes at most once on any
worker. DDL/RLS live in migration a0011; this model is metadata only.
"""

from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.models.identity_extensions import IdentityExtensionBase


class AcademyOidcLoginState(IdentityExtensionBase):
    __tablename__ = "academy_oidc_login_states"
    __table_args__ = (
        sa.UniqueConstraint("tenant_id", "state_hash", name="uq_academy_oidc_login_tenant_state"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["public.tenants.id"],
            name="fk_academy_oidc_login_tenant",
            ondelete="CASCADE",
        ),
        {"schema": "public"},
    )
    id: Mapped[UUID] = mapped_column(sa.Uuid, primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(sa.Uuid, nullable=False)
    state_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    provider_binding: Mapped[str] = mapped_column(sa.String(80), nullable=False)
    code_verifier: Mapped[str] = mapped_column(sa.Text, nullable=False)
    nonce: Mapped[str] = mapped_column(sa.Text, nullable=False)
    redirect_uri: Mapped[str] = mapped_column(sa.Text, nullable=False)
    return_to: Mapped[str] = mapped_column(sa.Text, nullable=False)
    issued_at: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
