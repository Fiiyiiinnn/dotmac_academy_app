"""Legacy-compat test fixtures (qualification harness for dotmac_academy_v2).

Adapted from the legacy Academy conftest: the database is a v2 genesis
database (kernel 0030 + lr_0012 + a0001-a0006) created by
`legacy_probe/genesis.py`; the platform-settings guard snapshots the
kernel `domain_settings` academy rows instead of the retired
`platform_settings` table.

Two-tenant setup: every isolation test gets `tenant_a` and `tenant_b` and a
`client_for(tenant)` helper that issues requests against the right subdomain.

Database tests require a real PostgreSQL with RLS. CI provisions a disposable
service; development database rehearsals use the dedicated test server.

Set `TEST_DATABASE_URL` before running database tests.
"""

from __future__ import annotations

import os
from collections.abc import Generator
from urllib.parse import quote
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import MetaData, Table, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

# Expose the genuine shared fixture chain in every requesting test module.
pytest_plugins = ("legacy_probe.canonical_identity_fixtures",)


def _missing_database(message):
    if os.environ.get("CI") == "true":
        pytest.fail(message)
    pytest.skip(message)


@pytest.fixture(scope="session")
def admin_engine():
    """Migration-authority connection (app_admin in CI).

    Bound to ``TEST_MIGRATION_DATABASE_URL`` (falling back to
    ``TEST_DATABASE_URL``), which in CI authenticates as the non-superuser
    ``app_admin`` migration role with BYPASSRLS. Dedicated worker privilege tests
    use the separate ``lab_worker_engine``/``lab_worker_session`` fixtures below.
    """
    url = os.getenv("TEST_MIGRATION_DATABASE_URL") or os.getenv("TEST_DATABASE_URL")
    if not url:
        _missing_database("TEST_DATABASE_URL not set — these tests require a real Postgres")
    engine = create_engine(url, future=True)
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def _set_database_url(monkeypatch):
    """Pin DATABASE_URL for the app under test to the TEST_DATABASE_URL."""
    url = os.getenv("TEST_DATABASE_URL")
    if url:
        monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("PLATFORM_ROOT_DOMAIN", "localhost")


@pytest.fixture(autouse=True)
def _no_real_console_spawn(monkeypatch):
    """A test may never launch a real ttyd. Enforced globally, not per-test.

    ``provision`` calls :func:`app.services.lab_lifecycle.start_console`, which
    ``Popen``s a real ttyd whenever the binary is installed — so on any host that
    has ttyd, a provision test spawned a daemon that outlived the run, while the
    test's rollback removed the row that would have identified it. That is exactly
    how the academy host accumulated 14 orphaned consoles, 4 of them spinning at
    99% CPU for up to 15 days.

    Patching per-test is not enough: the tests that leaked were the ones that
    forgot to. This fixture makes forgetting harmless, and the default return of
    ``None`` matches "console unavailable", which ``provision`` already tolerates.
    Tests that exercise ``start_console`` itself patch ``subprocess.Popen``
    directly and are unaffected by this.
    """
    from app.services import lab_lifecycle

    monkeypatch.setattr(lab_lifecycle, "start_console", lambda cname, base_path: None)


@pytest.fixture
def admin_session(admin_engine, platform_settings_guard) -> Generator[Session, None, None]:
    """Migration-authority session used by fixtures to set up cross-tenant data.

    Bound to ``admin_engine`` — see that fixture's docstring: this is the
    ``app_admin`` connection in CI.
    """
    SessionLocal = sessionmaker(bind=admin_engine, autocommit=False, autoflush=False)
    db = SessionLocal()
    try:
        yield db
        db.rollback()  # keep test DB clean — explicit commits required where needed
    finally:
        db.close()


def _platform_settings_rows(conn):
    result = conn.execute(text("SELECT * FROM public.domain_settings WHERE domain = 'academy'"))
    columns = list(result.keys())
    rows = sorted((tuple(row) for row in result), key=lambda row: tuple(map(repr, row)))
    return columns, rows


@pytest.fixture
def platform_settings_guard(admin_engine):
    """Undo any committed ``platform_settings`` change a database test leaves.

    ``platform_settings`` is global (not tenant scoped) and tenant teardown does
    not clear it, so a test that commits a policy value leaks it into every later
    test in the process. Snapshot the raw rows (all columns) on a separate
    migration-authority connection before the test and, only if they differ
    afterwards, restore them exactly in one transaction, then re-read and assert
    the restore is exact. The separate connection leaves the test's own session
    untouched.

    ``admin_session`` and ``app_user_session`` depend on this fixture, so every
    path to them (static or ``request.getfixturevalue``) snapshots first. pytest
    tears fixtures down in reverse setup order, so the restore runs after the
    session's rollback and close.
    """
    engine = admin_engine
    with engine.connect() as conn:
        columns, before = _platform_settings_rows(conn)
        conn.rollback()
    try:
        yield
    finally:
        with engine.connect() as conn:
            _, after = _platform_settings_rows(conn)
            conn.rollback()
        if after != before:
            with engine.begin() as conn:
                table = Table("domain_settings", MetaData(), schema="public", autoload_with=conn)
                conn.execute(table.delete().where(table.c.domain == "academy"))
                if before:
                    conn.execute(table.insert(), [dict(zip(columns, row, strict=True)) for row in before])
            with engine.connect() as conn:
                _, restored = _platform_settings_rows(conn)
                conn.rollback()
            assert restored == before, "domain_settings restore was not exact"


@pytest.fixture(scope="session")
def lab_worker_engine():
    """Real ``academy_lab_worker`` engine, bound exclusively to the test DSN.

    Skips cleanly when the env var is absent so local dev without a lab-worker
    DSN configured is unaffected. Deliberately does NOT fall back to
    ``TEST_MIGRATION_DATABASE_URL``/``TEST_DATABASE_URL`` like ``admin_engine``
    does — this fixture's whole purpose is to guarantee the connection actually
    authenticates as ``academy_lab_worker``, so a silent fallback to another role would
    defeat it.
    """
    url = os.getenv("TEST_LAB_WORKER_DATABASE_URL")
    if not url:
        _missing_database("TEST_LAB_WORKER_DATABASE_URL not set — lab-worker privilege tests require their role")
    engine = create_engine(url, future=True)
    with engine.connect() as conn:
        current_user = conn.execute(text("SELECT current_user")).scalar()
        if current_user != "academy_lab_worker":
            raise AssertionError(
                f"TEST_LAB_WORKER_DATABASE_URL authenticated as {current_user!r}, not academy_lab_worker"
            )
        bypassrls = conn.execute(
            text("SELECT rolbypassrls FROM pg_roles WHERE rolname = 'academy_lab_worker'")
        ).scalar()
        if not bypassrls:
            raise AssertionError("academy_lab_worker role does not have rolbypassrls = true")
        safe_posture = conn.execute(
            text(
                """SELECT NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole
                    AND NOT rolreplication AND NOT rolinherit
                    AND NOT EXISTS (
                        SELECT 1 FROM pg_auth_members m
                        WHERE m.member = r.oid OR m.roleid = r.oid
                    )
                FROM pg_roles r
                WHERE r.rolname = 'academy_lab_worker'"""
            )
        ).scalar()
        if not safe_posture:
            raise AssertionError("academy_lab_worker has unsafe attributes or memberships")
        owns = conn.execute(
            text(
                """SELECT EXISTS (
                    SELECT 1 FROM pg_database
                    WHERE datname = current_database()
                      AND pg_get_userbyid(datdba) = 'academy_lab_worker'
                ) OR EXISTS (
                    SELECT 1 FROM pg_namespace
                    WHERE pg_get_userbyid(nspowner) = 'academy_lab_worker'
                      AND nspname NOT IN ('pg_catalog', 'information_schema')
                      AND nspname NOT LIKE 'pg_toast%'
                ) OR EXISTS (
                    SELECT 1 FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE pg_get_userbyid(c.relowner) = 'academy_lab_worker'
                      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
                      AND n.nspname NOT LIKE 'pg_toast%'
                      AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
                )"""
            )
        ).scalar()
        if owns:
            raise AssertionError("academy_lab_worker must not own database, schema, or application relations")
    yield engine
    engine.dispose()


@pytest.fixture
def lab_worker_session(lab_worker_engine) -> Generator[Session, None, None]:
    """Real dedicated worker session for asserting the live ACL matrix."""
    SessionLocal = sessionmaker(bind=lab_worker_engine, autocommit=False, autoflush=False)
    db = SessionLocal()
    try:
        yield db
        db.rollback()
    finally:
        db.close()


@pytest.fixture
def app_user_session(platform_settings_guard) -> Generator[Session, None, None]:
    """Connection as app_user for RLS visibility assertions."""
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        _missing_database("TEST_DATABASE_URL not set — these tests require a real Postgres")
    engine = create_engine(url, future=True)
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    db = SessionLocal()
    try:
        yield db
        db.rollback()
    finally:
        db.close()
        engine.dispose()


def _make_tenant(admin_session: Session, slug: str, name: str):
    from app.models.tenant import Tenant

    # Self-heal: clear any aborted-transaction state and any leftover tenant with
    # this slug (from an interrupted run, or a prior test that left the session
    # aborted — psycopg silently ignores the teardown DELETE in that case, so the
    # row survives and the next test collides on the unique slug).
    admin_session.rollback()
    admin_session.execute(text("DELETE FROM tenants WHERE slug = :s"), {"s": slug})
    admin_session.commit()
    t = Tenant(slug=slug, name=name)
    admin_session.add(t)
    admin_session.commit()
    admin_session.refresh(t)
    return t


def _drop_tenant(admin_session: Session, t) -> None:
    admin_session.rollback()  # clear any aborted tx the test left behind
    admin_session.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": str(t.id)})
    admin_session.commit()


@pytest.fixture
def tenant_a(admin_session: Session):
    t = _make_tenant(admin_session, "alpha", "Alpha Test Tenant")
    yield t
    _drop_tenant(admin_session, t)


@pytest.fixture
def tenant_b(admin_session: Session):
    t = _make_tenant(admin_session, "beta", "Beta Test Tenant")
    yield t
    _drop_tenant(admin_session, t)


@pytest.fixture
def app_client():
    """TestClient that lets you set Host header per request."""
    from app.main import app

    return TestClient(app)


def client_for(client: TestClient, tenant_slug: str) -> TestClient:
    """Wrap a TestClient so every request carries Host: {slug}.localhost."""
    client.headers.update({"Host": f"{tenant_slug}.localhost"})
    return client


def login_location(path: str) -> str:
    """Exact kernel redirect for an unauthenticated request to ``path``."""
    return "/login?next=" + quote(path, safe="")


def csrf_for(client: TestClient, headers, path: str) -> str:
    """GET a safe browser page and return the session-bound ``csrf_token``.

    The kernel CSRF token is bound to the current ``session`` cookie. After a
    login the session cookie changes, so the pre-login token is invalid; the
    kernel issues a fresh token on a safe request to a route that carries
    ``require_csrf``. CSRF stays enabled; this only reads the freshly issued
    token.
    """
    response = client.get(path, headers=dict(headers), follow_redirects=False)
    assert response.status_code < 400, f"GET {path} returned {response.status_code}"
    token = client.cookies.get("csrf_token", "")
    assert token, f"csrf_token cookie not set after GET {path}"
    return token


def web_login(client: TestClient, host, email: str, password: str, *, refresh_path: str = "/account"):
    """Log in through the real web form with the kernel's double-submit CSRF.

    ``host`` is the Host header value or a headers mapping carrying ``Host``.
    GET /login issues the ``csrf_token`` cookie; the POST echoes it in the
    ``x-csrf-token`` header (CSRF stays enabled). Asserts the 303 success redirect.

    The token is session-bound, so login invalidates it. After the 303 this does
    one GET of ``refresh_path`` (an authenticated browser page) and asserts the
    ``csrf_token`` cookie was re-issued, so later unsafe requests that read
    ``client.cookies["csrf_token"]`` carry a valid token.
    """
    headers = {"Host": host} if isinstance(host, str) else dict(host)
    page = client.get("/login", headers=headers, follow_redirects=False)
    csrf = page.cookies.get("csrf_token") or client.cookies.get("csrf_token", "")
    assert csrf, "csrf_token cookie not set by GET /login"
    response = client.post(
        "/login",
        headers={**headers, "x-csrf-token": csrf},
        data={"email": email, "password": password},
        follow_redirects=False,
    )
    assert response.status_code == 303, f"login for {email!r} returned {response.status_code}"
    fresh = csrf_for(client, headers, refresh_path)
    assert fresh != csrf, "csrf_token was not re-issued for the logged-in session"
    return response


def make_canonical_account(
    db: Session,
    *,
    tenant_id,
    email: str,
    first_name: str,
    last_name: str,
    password: str,
    role: str,
):
    """Create one genuine canonical account (Party/PartyPerson/profile/
    credential/policy/grant) for tests with no web request to authorize via.

    Mirrors the canonical fixtures in tests/services/test_canonical_lifecycle.py
    plus a role grant; ``create_web_user`` is not drop-in here because it
    requires a live actor cookie. Caller owns commit/rollback.
    """
    from datetime import UTC, datetime

    from dotmac_kernel.models import Party, PartyPerson, PartyRoleGrant, PartyType, UserCredential

    from app.models.identity_extensions import AcademyAccountProfile, AcademyCredentialPolicy
    from app.services.bootstrap import ensure_roles
    from app.services.identity import canonical_party_for_email, normalize_email
    from app.services.security import hash_password

    roles = ensure_roles(db, tenant_id)
    if role not in roles:
        raise ValueError(f"Invalid role {role!r}; expected one of {sorted(roles)}")
    email = normalize_email(email)
    if canonical_party_for_email(db, tenant_id=tenant_id, email=email) is not None:
        raise ValueError(f"A person with email {email!r} already exists in this tenant")
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
            tenant_id=tenant_id, party_id=party.id, status="active",
            prefs={}, created_at=now, updated_at=now,
        )
    )
    credential = UserCredential(
        tenant_id=tenant_id, party_id=party.id, password_hash=hash_password(password)
    )
    db.add(credential)
    db.flush()
    db.add(
        AcademyCredentialPolicy(
            tenant_id=tenant_id, credential_id=credential.id, email=email,
            failed_login_attempts=0, locked_until=None, lifecycle_version=0,
            reset_required=False, created_at=now, updated_at=now,
        )
    )
    db.add(PartyRoleGrant(tenant_id=tenant_id, party_id=party.id, role_id=roles[role].id))
    db.flush()
    return party


def make_person_party(
    db: Session,
    *,
    tenant_id,
    email: str,
    first_name: str,
    last_name: str,
    status: str = "active",
    prefs=None,
    avatar_path: str | None = None,
):
    """Canonical stand-in for a retired ``Person`` row in test fixtures.

    Creates the Party + PartyPerson subtype + AcademyAccountProfile triple
    (the composed owners of the legacy person's email/names and of its
    status/prefs/avatar columns) and returns the Party, whose ``id`` is the
    ``person_id`` every Learning/retained table now references. No credential
    or role grant is created — add those explicitly (see
    ``make_person_credential`` / ``PartyRoleGrant``) or use
    ``make_canonical_account`` for a full login-capable account. Caller owns
    commit/rollback.
    """
    from datetime import UTC, datetime

    from dotmac_kernel.models import Party, PartyPerson, PartyType

    from app.models.identity_extensions import AcademyAccountProfile

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
            tenant_id=tenant_id,
            party_id=party.id,
            status=status,
            avatar_path=avatar_path,
            prefs={} if prefs is None else prefs,
            created_at=now,
            updated_at=now,
        )
    )
    db.flush()
    return party


def grant_role(db: Session, *, tenant_id, party_id, role: str = "student"):
    """Grant a standard role to an existing Party (idempotent).

    The web facet admits only the default roles (student/instructor/admin), so a
    login helper that creates a Party and credential without a grant gets 403 on
    every guarded page. Do not call this where a test asserts that a role-less
    user is denied. Caller owns commit/rollback.
    """
    from dotmac_kernel.models import PartyRoleGrant
    from sqlalchemy import select

    from app.services.bootstrap import ensure_roles

    roles = ensure_roles(db, tenant_id)
    if role not in roles:
        raise ValueError(f"Invalid role {role!r}; expected one of {sorted(roles)}")
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


def make_person_credential(
    db: Session,
    *,
    tenant_id,
    party_id,
    email: str,
    password_hash: str,
):
    """Canonical stand-in for a legacy ``UserCredential(person_id=, email=)`` row.

    The kernel credential carries ``party_id`` only; the login email and
    lockout/reset bookkeeping live on AcademyCredentialPolicy. Returns the
    credential. Caller owns commit/rollback.
    """
    from datetime import UTC, datetime

    from dotmac_kernel.models import UserCredential

    from app.models.identity_extensions import AcademyCredentialPolicy

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


@pytest.fixture
def api_actor(admin_session):
    """Create a real tenant account and return its authenticated API context."""

    def _create(client: TestClient, tenant, *, role: str = "admin", email: str | None = None):
        password = "correct horse battery staple"
        email = email or f"{role}-{uuid4().hex[:10]}@test.example"
        admin_session.rollback()
        person = make_canonical_account(
            admin_session,
            tenant_id=tenant.id,
            email=email,
            first_name=role.title(),
            last_name="Tester",
            password=password,
            role=role,
        )
        admin_session.commit()
        admin_session.refresh(person)

        scoped = client_for(client, tenant.slug)
        response = scoped.post(
            "/auth/login",
            json={"email": email, "password": password},
        )
        assert response.status_code == 200, response.text
        token = response.json()["access_token"]
        return {
            "headers": {"Authorization": f"Bearer {token}"},
            "person": person,
            "token": token,
        }

    return _create
