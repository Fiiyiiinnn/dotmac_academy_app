"""Wave-3 (authorization port) shared fixtures over ``genesis_database``.

Rebuilds the legacy probe's ``current_account``/``tenant_b``/``app_client``
surface against the maintained per-test genesis database:

- ``a.admin`` is a session on the genesis ``app_admin`` engine (setup and
  independent committed mutations), ``a.db`` a tenant-scoped ``app_user``
  session (the request-side session whose identity map the stale-read
  proofs observe), ``a.platform`` a ``platform_api`` session factory.
- The account is a genuine canonical sign-in (Party/PartyPerson/profile/
  credential/policy) through ``app.services.credential_authentication``.
- Route tests compose a bare FastAPI app from the real product routers
  with the tenant placed on ``request.state`` and ``get_db`` /
  ``get_platform_db`` overridden to the genesis engines (the pattern the
  legacy ``test_product_admin_routes_owner`` already used). The kernel
  facet middleware of the full assembly is out of scope for these
  owner-level ports, so an invalid cookie surfaces as
  ``require_web_user``'s own 303 to ``/login`` rather than the facet 302.

Each test gets a fresh genesis database, so the legacy
platform-settings restore fixtures are unnecessary: ``domain_settings``
starts empty.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import quote
from uuid import uuid4

import pytest
from dotmac_kernel.models import Party, PartyPerson, PartyRoleGrant, PartyType, Role, Tenant, UserCredential
from dotmac_kernel.settings_models import DomainSetting
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import Request

from app.api.deps import get_db, get_platform_db
from app.models.identity_extensions import AcademyAccountProfile, AcademyCredentialPolicy
from app.services.bootstrap import ensure_roles
from app.services.credential_authentication import sign_in
from app.services.security import hash_password


def _engine(superuser: str, dbname: str, user: str):
    url = make_url(superuser).set(database=dbname, username=user, password=None)
    return create_engine(url.render_as_string(hide_password=False), future=True)


def _session(engine) -> Session:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()


def scope(db: Session, tenant_id) -> None:
    """Session-level tenant scope (survives the harness's request commits)."""
    db.execute(
        text("SELECT set_config('app.current_tenant', :tenant, false)"),
        {"tenant": str(tenant_id)},
    )


def make_tenant(admin: Session, slug: str, name: str) -> Tenant:
    tenant = Tenant(slug=slug, name=name)
    admin.add(tenant)
    admin.commit()
    return tenant


def make_person_party(db: Session, *, tenant_id, email, first_name, last_name, status="active"):
    now = datetime.now(UTC)
    party = Party(
        tenant_id=tenant_id,
        party_type=PartyType.person,
        display_name=f"{first_name} {last_name}".strip(),
        email=email,
        is_active=True,
    )
    db.add(party)
    db.flush()
    db.add(PartyPerson(party_id=party.id, first_name=first_name, last_name=last_name))
    db.flush()
    db.add(
        AcademyAccountProfile(
            tenant_id=tenant_id, party_id=party.id, status=status, prefs={}, created_at=now, updated_at=now
        )
    )
    db.flush()
    return party


def make_person_credential(db: Session, *, tenant_id, party_id, email, password_hash):
    now = datetime.now(UTC)
    credential = UserCredential(tenant_id=tenant_id, party_id=party_id, password_hash=password_hash)
    db.add(credential)
    db.flush()
    db.add(
        AcademyCredentialPolicy(
            tenant_id=tenant_id,
            credential_id=credential.id,
            email=email,
            failed_login_attempts=0,
            locked_until=None,
            lifecycle_version=0,
            reset_required=False,
            created_at=now,
            updated_at=now,
        )
    )
    db.flush()
    return credential


def grant_std(db: Session, *, tenant_id, party_id, role: str = "student"):
    """Grant a standard role (legacy ``grant_role``); flush only."""
    roles = ensure_roles(db, tenant_id)
    existing = db.scalar(
        select(PartyRoleGrant).where(
            PartyRoleGrant.tenant_id == tenant_id,
            PartyRoleGrant.party_id == party_id,
            PartyRoleGrant.role_id == roles[role].id,
        )
    )
    if existing is not None:
        return existing
    grant = PartyRoleGrant(tenant_id=tenant_id, party_id=party_id, role_id=roles[role].id)
    db.add(grant)
    db.flush()
    return grant


def grant_custom(a, slug, tenant_id=None):
    """Legacy ``_grant``: custom purpose role plus the standard student role
    the web facet requires; commits on the admin session. Returns grant id."""
    if tenant_id is None and slug not in {"student", "instructor", "admin"}:
        grant_std(a.admin, tenant_id=a.tid, party_id=a.pid)
    tid = tenant_id or a.tid
    role = a.admin.scalars(select(Role).where(Role.tenant_id == tid, Role.slug == slug)).first()
    if role is None:
        role = Role(tenant_id=tid, slug=slug, name=slug)
        a.admin.add(role)
        a.admin.flush()
    grant = PartyRoleGrant(tenant_id=tid, party_id=a.pid, role_id=role.id)
    a.admin.add(grant)
    a.admin.commit()
    return grant.id


def party_only(a, tenant_id):
    """Legacy ``_party``: Party + PartyPerson, no profile; flush only."""
    party = Party(
        tenant_id=tenant_id,
        party_type=PartyType.person,
        display_name="Recipient",
        email=f"notification-{uuid4().hex}@example.test",
        is_active=True,
    )
    a.admin.add(party)
    a.admin.flush()
    a.admin.add(PartyPerson(party_id=party.id, first_name="Notification", last_name="Recipient"))
    a.admin.flush()
    return party


def raw_setting_row(db: Session, key: str):
    return db.scalars(
        select(DomainSetting).where(
            DomainSetting.domain == "academy",
            DomainSetting.scope_kind == "platform",
            DomainSetting.tenant_id.is_(None),
            DomainSetting.key == key,
        )
    ).one_or_none()


def store_raw_setting(admin: Session, key: str, raw) -> None:
    """Insert/replace a raw stored ``domain_settings`` value (commits).

    Text raws land in ``value_text`` exactly as a legacy
    ``PlatformSetting.value`` did. ``raw=None`` stores a JSON ``null``
    (kernel storage forbids an all-NULL row), which the store surfaces
    as a retained NULL policy value for the strict validators to refuse.
    """
    existing = raw_setting_row(admin, key)
    if existing is not None:
        admin.delete(existing)
        admin.flush()
    if raw is None:
        admin.execute(
            text(
                "INSERT INTO public.domain_settings "
                "(id, tenant_id, scope_kind, scope_id, domain, key, value_type, "
                "value_text, value_json, is_secret, is_active) VALUES "
                "(:id, NULL, 'platform', NULL, 'academy', :key, 'json', NULL, "
                "CAST('null' AS jsonb), false, true)"
            ),
            {"id": uuid4(), "key": key},
        )
    else:
        admin.add(
            DomainSetting(
                tenant_id=None, scope_kind="platform", domain="academy", key=key, value_text=raw, value_json=None
            )
        )
    admin.commit()


def update_raw_setting(admin: Session, key: str, raw: str) -> None:
    """Replace a stored value as text (or as a stored JSON list for '[]')."""
    store_raw_setting(admin, key, raw)


def login_location(path: str) -> str:
    return "/login?next=" + quote(path, safe="")


def make_request(tenant, token=None, path: str = "/bookmarks") -> Request:
    headers = [] if token is None else [(b"cookie", f"session={token}".encode())]
    request = Request({"type": "http", "headers": headers, "state": {"tenant": tenant}})
    request.scope.update(
        {
            "scheme": "https",
            "server": ("alpha.localhost", 443),
            "path": path,
            "root_path": "",
            "query_string": b"",
            "method": "GET",
        }
    )
    return request


def make_client(a, *routers) -> TestClient:
    app = FastAPI()

    # The full kernel assembly registers dotmac_kernel.errors handlers
    # translating DomainError subclasses to HTTP statuses; mirror that
    # mapping here so route assertions see assembly status codes.
    from dotmac_kernel.exceptions import (
        BadRequestError,
        ConflictError,
        ForbiddenError,
        NotFoundError,
        UnauthorizedError,
    )
    from fastapi.responses import JSONResponse

    for exc_type, status in (
        (NotFoundError, 404),
        (BadRequestError, 400),
        (ConflictError, 409),
        (ForbiddenError, 403),
        (UnauthorizedError, 401),
    ):

        def _handler(request, exc, status=status):
            return JSONResponse(status_code=status, content={"detail": str(exc)})

        app.add_exception_handler(exc_type, _handler)

    @app.middleware("http")
    async def _tenant(request, call_next):
        request.state.tenant = a.tenant
        return await call_next(request)

    def _db():
        scope(a.db, a.tid)
        try:
            yield a.db
            a.db.commit()
        except Exception:
            a.db.rollback()
            raise

    def _platform_db():
        session = _session(a.platform_engine)
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_platform_db] = _platform_db
    for router in routers:
        app.include_router(router)
    client = TestClient(app, follow_redirects=False)
    client.cookies.set("session", a.token)
    return client


@pytest.fixture
def current_account(genesis_database):
    admin_engine, superuser, dbname = genesis_database
    app_engine = _engine(superuser, dbname, "app_user")
    platform_engine = _engine(superuser, dbname, "platform_api")
    admin = _session(admin_engine)
    db = _session(app_engine)
    tenant_a = make_tenant(admin, f"alpha-{uuid4().hex[:12]}", "Alpha Test Tenant")
    tenant_b = make_tenant(admin, f"beta-{uuid4().hex[:12]}", "Beta Test Tenant")
    email = f"signin-{uuid4().hex}@a.edu"
    party = make_person_party(admin, tenant_id=tenant_a.id, email=email, first_name="Sign", last_name="In")
    credential = make_person_credential(
        admin, tenant_id=tenant_a.id, party_id=party.id, email=email, password_hash=hash_password("chosen-password9")
    )
    admin.commit()
    scope(db, tenant_a.id)
    result = sign_in(db, tenant_id=tenant_a.id, email=email, password="chosen-password9")
    assert result is not None
    db.commit()
    scope(db, tenant_a.id)
    assert db.scalar(text("SELECT current_user")) == "app_user"
    account = SimpleNamespace(
        db=db,
        admin=admin,
        tenant=tenant_a,
        tenant_b=tenant_b,
        tid=tenant_a.id,
        pid=party.id,
        email=email,
        credential_id=credential.id,
        token=result.session_token,
        session_id=result.session_id,
        login_snapshot=result.account,
        app_engine=app_engine,
        platform_engine=platform_engine,
    )
    try:
        yield account
    finally:
        db.rollback()
        admin.rollback()
        db.close()
        admin.close()
        app_engine.dispose()
        platform_engine.dispose()


@pytest.fixture
def tenant_b(current_account):
    return current_account.tenant_b
