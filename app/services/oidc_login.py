"""Keycloak (OIDC) staff login for Academy — pilot cohort only.

Three owners, kept separate (the `dotmac-auth-oidc` boundary):

    dotmac_auth_oidc                 -> verified (issuer, subject)
    dotmac_kernel.external_identity  -> which local Party is that, here?
    this module                      -> staff gate + Academy session

No just-in-time provisioning and no email matching: a subject only
logs in through an explicit `ExternalIdentityBinding` an operator
created beforehand (`oidc-bind` CLI). Provider claims grant nothing;
authorization stays with Academy's local role grants. Local password
login is untouched and remains available to every account.

The pilot cohort is staff: only parties holding a configured staff
role (`oidc_staff_roles`, default instructor/admin) may complete this
flow, at both bind time and login time.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Final
from uuid import UUID, uuid4

from dotmac_auth_oidc import PER_REQUEST_STATE_STORE, LoginState, OIDCClient, RelyingPartyConfig
from dotmac_auth_oidc.errors import OIDCError
from dotmac_kernel.external_identity import (
    bind_external_identity,
    disable_external_identity_binding,
    finalize_external_login,
)
from dotmac_kernel.models import AuthSession, ExternalIdentityBinding, Party, PartyRoleGrant, Role, Tenant
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.config import settings
from app.services.identity import AcademyCurrentAccount, current_account
from app.services.security import hash_token, issue_access_token

logger = logging.getLogger(__name__)

STATE_COOKIE: Final[str] = "academy_oidc_state"
_CEREMONY_TABLE: Final[str] = "public.academy_oidc_login_states"

_SWEEP_SQL: Final[str] = (
    f"DELETE FROM {_CEREMONY_TABLE} "  # noqa: S608 - module constant, values bound
    "WHERE tenant_id = :tenant_id AND expires_at <= now()"
)
_INSERT_SQL: Final[str] = (
    f"INSERT INTO {_CEREMONY_TABLE} ("  # noqa: S608 - module constant, values bound
    " id, tenant_id, state_hash, provider_binding, code_verifier, nonce,"
    " redirect_uri, return_to, issued_at, expires_at"
    ") VALUES ("
    " :id, :tenant_id, :state_hash, :provider_binding, :code_verifier, :nonce,"
    " :redirect_uri, :return_to, :issued_at, :expires_at"
    ")"
)
_CONSUME_SQL: Final[str] = (
    f"DELETE FROM {_CEREMONY_TABLE} "  # noqa: S608 - module constant, values bound
    "WHERE tenant_id = :tenant_id "
    "  AND state_hash = :state_hash "
    "  AND expires_at > now() "
    "RETURNING code_verifier, nonce, redirect_uri, return_to, issued_at, provider_binding"
)


class OidcNotConfigured(RuntimeError):
    """Federated login is disabled or incompletely configured."""


class OidcLoginRefused(Exception):
    """The ceremony did not produce a session. One shape, no reason field."""


@dataclass(frozen=True, slots=True)
class StartedOidcLogin:
    url: str
    state: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class CompletedOidcLogin:
    account: AcademyCurrentAccount
    session_token: str
    binding_id: UUID


def oidc_enabled() -> bool:
    return bool(settings.oidc_enabled)


def provider_binding() -> str:
    return settings.oidc_provider_binding


def _state_hash(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


class AcademyOidcStateStore:
    """Shared atomic ceremony store over the request's own session.

    `take` is one DELETE ... RETURNING, so exactly one callback on any
    worker can claim a ceremony. Constructed per request: the caller's
    transaction owns when the ceremony commits.
    """

    __slots__ = ("_binding", "_db", "_tenant_id")

    def __init__(self, db: Session, *, tenant_id: UUID, provider_binding: str) -> None:
        self._db = db
        self._tenant_id = tenant_id
        self._binding = provider_binding

    def put(self, state: LoginState, *, ttl_seconds: int) -> None:
        self._db.execute(text(_SWEEP_SQL), {"tenant_id": str(self._tenant_id)})
        self._db.execute(
            text(_INSERT_SQL),
            {
                "id": str(uuid4()),
                "tenant_id": str(self._tenant_id),
                "state_hash": _state_hash(state.state_id),
                "provider_binding": self._binding,
                "code_verifier": state.code_verifier,
                "nonce": state.nonce,
                "redirect_uri": state.redirect_uri,
                "return_to": state.return_to,
                "issued_at": state.issued_at,
                "expires_at": datetime.now(UTC) + timedelta(seconds=ttl_seconds),
            },
        )

    def take(self, state_id: str) -> LoginState | None:
        row = (
            self._db.execute(
                text(_CONSUME_SQL),
                {"tenant_id": str(self._tenant_id), "state_hash": _state_hash(state_id)},
            )
            .mappings()
            .first()
        )
        if row is None or row["provider_binding"] != self._binding:
            return None
        return LoginState(
            state_id=state_id,
            nonce=row["nonce"],
            code_verifier=row["code_verifier"],
            redirect_uri=row["redirect_uri"],
            issued_at=int(row["issued_at"]),
            return_to=row["return_to"],
        )


@lru_cache(maxsize=1)
def build_oidc_client() -> OIDCClient:
    """The process-lifetime relying-party client (owns the provider cache).

    Built once from deployment settings; the ceremony store is supplied
    per request. Raises OidcNotConfigured when federated login is off or
    the client secret is absent — a half-configured front door must fail
    here, not at the first staff login.
    """
    if not settings.oidc_enabled or not settings.oidc_client_secret:
        raise OidcNotConfigured("OIDC federated login is not configured")
    return OIDCClient(
        RelyingPartyConfig(
            provider_binding=settings.oidc_provider_binding,
            issuer=settings.oidc_issuer,
            client_id=settings.oidc_client_id,
            client_secret=settings.oidc_client_secret,
            redirect_uri=settings.oidc_redirect_url,
            scopes=("openid",),
        ),
        state_store=PER_REQUEST_STATE_STORE,
        timeout=settings.oidc_http_timeout_seconds,
    )


def _staff_roles(db: Session, *, tenant_id: UUID, party_id: UUID) -> set[str]:
    return set(
        db.scalars(
            select(Role.slug)
            .join(
                PartyRoleGrant,
                (PartyRoleGrant.role_id == Role.id) & (PartyRoleGrant.tenant_id == Role.tenant_id),
            )
            .where(
                PartyRoleGrant.tenant_id == tenant_id,
                PartyRoleGrant.party_id == party_id,
                Role.tenant_id == tenant_id,
            )
            .execution_options(populate_existing=True)
        ).all()
    )


def _is_staff(db: Session, *, tenant_id: UUID, party_id: UUID) -> bool:
    return bool(_staff_roles(db, tenant_id=tenant_id, party_id=party_id) & set(settings.oidc_staff_roles))


def bind_staff_identity(
    db: Session, *, tenant: Tenant, party: Party, subject: str, bound_by: str, reason: str
) -> ExternalIdentityBinding:
    """Operator act: bind one exact Keycloak subject to one staff party."""
    if not _is_staff(db, tenant_id=tenant.id, party_id=party.id):
        raise ValueError("OIDC pilot binding requires a staff role (instructor or admin)")
    return bind_external_identity(
        db,
        tenant=tenant,
        party=party,
        provider_binding=settings.oidc_provider_binding,
        issuer=settings.oidc_issuer,
        subject=subject,
        bound_by=bound_by,
        reason=reason,
    )


def disable_staff_identities(db: Session, *, tenant: Tenant, party_id: UUID) -> int:
    """Disable this party's Academy OIDC bindings (revokes their sessions)."""
    bindings = db.scalars(
        select(ExternalIdentityBinding).where(
            ExternalIdentityBinding.tenant_id == tenant.id,
            ExternalIdentityBinding.party_id == party_id,
            ExternalIdentityBinding.provider_binding == settings.oidc_provider_binding,
            ExternalIdentityBinding.issuer == settings.oidc_issuer,
        )
    ).all()
    for binding in bindings:
        disable_external_identity_binding(db, tenant=tenant, binding_id=binding.id)
    return len(bindings)


def begin_login(db: Session, *, tenant: Tenant, client: OIDCClient | None = None) -> StartedOidcLogin:
    client = client or build_oidc_client()
    store = AcademyOidcStateStore(db, tenant_id=tenant.id, provider_binding=settings.oidc_provider_binding)
    try:
        redirect = client.start_login(return_to="/", ttl_seconds=settings.oidc_ceremony_ttl_seconds, state_store=store)
    except OIDCError as exc:
        logger.warning("OIDC login could not start for tenant %s: %s", tenant.id, type(exc).__name__)
        raise OidcLoginRefused from None
    return StartedOidcLogin(
        url=redirect.url,
        state=redirect.state,
        expires_at=datetime.now(UTC) + timedelta(seconds=settings.oidc_ceremony_ttl_seconds),
    )


def complete_login(
    db: Session,
    *,
    tenant: Tenant,
    code: str,
    state: str,
    stored_state: str | None,
    client: OIDCClient | None = None,
) -> CompletedOidcLogin:
    client = client or build_oidc_client()
    store = AcademyOidcStateStore(db, tenant_id=tenant.id, provider_binding=settings.oidc_provider_binding)
    try:
        subject = client.complete_login(
            code=code,
            state_parameter=state,
            stored_state=stored_state or "",
            ttl_seconds=settings.oidc_ceremony_ttl_seconds,
            state_store=store,
        )
    except OIDCError as exc:
        logger.warning("OIDC login refused for tenant %s: %s", tenant.id, type(exc).__name__)
        raise OidcLoginRefused from None
    resolved = finalize_external_login(
        db,
        tenant=tenant,
        provider_binding=settings.oidc_provider_binding,
        issuer=subject.issuer,
        subject=subject.subject,
    )
    if resolved is None:
        logger.warning("OIDC login refused for tenant %s: unbound subject", tenant.id)
        raise OidcLoginRefused
    party = resolved.party
    # Staff-cohort gate, checked while the finalizer's locks are held and
    # in the same transaction that mints the session below.
    if not _is_staff(db, tenant_id=tenant.id, party_id=party.id):
        logger.warning("OIDC login refused for tenant %s: non-staff binding", tenant.id)
        raise OidcLoginRefused
    account = current_account(db, tenant_id=tenant.id, party_id=party.id)
    if account is None:
        logger.warning("OIDC login refused for tenant %s: inactive account", tenant.id)
        raise OidcLoginRefused
    token, expires_at = issue_access_token(party.id, tenant.id)
    db.add(
        AuthSession(
            tenant_id=tenant.id,
            party_id=party.id,
            external_identity_binding_id=resolved.binding_id,
            token_hash=hash_token(token),
            expires_at=expires_at,
        )
    )
    db.flush()
    return CompletedOidcLogin(account=account, session_token=token, binding_id=resolved.binding_id)
