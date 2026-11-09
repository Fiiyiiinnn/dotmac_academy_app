"""Wave-1 issue #2 fixtures: legacy hosted fixtures on maintained genesis.

Ports the legacy_probe conftest fixture chain (admin/app_user/lab-worker
sessions, two tenants, genuine canonical accounts) onto the maintained
per-test ``genesis_database`` fixture from tests/conftest.py. Role engines
are derived from the genesis superuser URL exactly as
tests/test_lab_operations.py does. The retired ``platform_settings``
storage is replaced by kernel ``domain_settings`` through
``app.services.settings_store``; per-test databases make the legacy
settings snapshot/restore guard unnecessary.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from dotmac_kernel.models import Party, PartyPerson, PartyType, UserCredential
from dotmac_kernel.session_runtime import resolve_database_runtime
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from app.config import LabExecutionPolicy, settings
from app.models.identity_extensions import AcademyAccountProfile, AcademyCredentialPolicy
from app.services import settings_store
from app.services.security import hash_password, hash_token
from tests.conftest import _url_for

os.environ.setdefault("PLATFORM_ROOT_DOMAIN", "localhost")


@pytest.fixture
def admin_engine(genesis_database):
    admin, _superuser, _name = genesis_database
    return admin


@pytest.fixture
def admin_session(admin_engine):
    factory = sessionmaker(bind=admin_engine, autocommit=False, autoflush=False)
    db = factory()
    try:
        yield db
        db.rollback()
    finally:
        db.close()


def _role_session(genesis_database, user):
    _admin, superuser, name = genesis_database
    engine = create_engine(_url_for(superuser, name, user=user), future=True)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    db = factory()
    try:
        yield db
        db.rollback()
    finally:
        db.close()
        engine.dispose()


@pytest.fixture
def app_user_session(genesis_database):
    yield from _role_session(genesis_database, "app_user")


@pytest.fixture
def lab_worker_engine(genesis_database):
    _admin, superuser, name = genesis_database
    engine = create_engine(_url_for(superuser, name, user="academy_lab_worker"), future=True)
    with engine.connect() as conn:
        current_user = conn.execute(text("SELECT current_user")).scalar()
        assert current_user == "academy_lab_worker", current_user
    yield engine
    engine.dispose()


@pytest.fixture
def lab_worker_session(lab_worker_engine):
    factory = sessionmaker(bind=lab_worker_engine, autocommit=False, autoflush=False)
    db = factory()
    try:
        yield db
        db.rollback()
    finally:
        db.close()


def _make_tenant(admin_session, slug, name):
    from app.models.tenant import Tenant

    admin_session.rollback()
    tenant = Tenant(slug=slug, name=name)
    admin_session.add(tenant)
    admin_session.commit()
    admin_session.refresh(tenant)
    return tenant


@pytest.fixture
def tenant_a(admin_session):
    return _make_tenant(admin_session, "alpha", "Alpha Test Tenant")


@pytest.fixture
def tenant_b(admin_session):
    return _make_tenant(admin_session, "beta", "Beta Test Tenant")


@pytest.fixture
def app_client():
    """Importing app.main binds the kernel runtime and audit registry."""
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


@pytest.fixture
def policy():
    """Explicit validated env/default snapshot; never a production fallback."""
    return LabExecutionPolicy.from_settings(settings)


LAB_POLICY_KEYS = (
    "lab_execution_policy_overrides",
    "branding_name",
    "max_concurrent_labs",
    "max_concurrent_labs_per_tenant",
    "lab_idle_minutes",
)


@pytest.fixture
def lab_policy_rows(admin_session):
    """DomainSetting port of the legacy PlatformSetting cleanup fixture."""
    for key in LAB_POLICY_KEYS:
        settings_store.delete(admin_session, key)
    admin_session.commit()
    yield admin_session


def no_real_console_spawn(monkeypatch):
    from app.services import lab_lifecycle

    monkeypatch.setattr(lab_lifecycle, "start_console", lambda cname, base_path: None)


def seed_lab(db, tenant_id, *, name="one", status="queued", presence=None):
    """Port of legacy ``_seed``: course/activity/template/person/instance."""
    from dotmac_learning import ActivityWrite, CourseWrite, create_activity, create_course

    from app.models.lab import LabInstance, LabTemplate

    course = create_course(
        db,
        CourseWrite(
            id=uuid4(),
            tenant_id=tenant_id,
            slug=f"worker-{name}",
            title="Worker",
            discipline="networking",
            source_ref="test",
            version=1,
            status="published",
            listed=False,
        ),
    )
    activity = create_activity(
        db,
        ActivityWrite(
            id=uuid4(),
            tenant_id=tenant_id,
            course_id=course.id,
            chapter_number=1,
            type="lab",
            title="Worker lab",
            pass_threshold=0.5,
            assessment_mode="graded",
            grading="auto",
            weight=1.0,
        ),
    )
    template = LabTemplate(
        tenant_id=tenant_id,
        course_id=course.id,
        chapter_number=1,
        activity_id=activity.id,
        slug=f"worker-{name}",
        title="Worker lab",
        topology="name: placeholder",
        instructions_html="<p>test</p>",
        checks=[],
        seed_spec={},
        limits={},
        source_hash="test",
        version=1,
    )
    person = Party(
        tenant_id=tenant_id,
        party_type=PartyType.person,
        display_name="Lab Worker",
        email=f"worker-{name}@example.test",
        is_active=True,
    )
    db.add_all([template, person])
    db.flush()
    db.add(PartyPerson(party_id=person.id, first_name="Lab", last_name="Worker"))
    db.flush()
    instance = LabInstance(
        tenant_id=tenant_id,
        activity_id=activity.id,
        person_id=person.id,
        instance_name=f"dal-worker-{name}",
        seed={},
        status=status,
        consoles={},
    )
    if presence is None:
        presence = "present" if status in ("provisioning", "active", "resetting") else "absent"
    instance.runtime_presence = presence
    db.add(instance)
    db.flush()
    return instance, person


def _canonical_party(setup, tenant_id, *, display_name, first_name, last_name, email, password, lifecycle_version=0):
    now = datetime.now(UTC)
    party = Party(
        tenant_id=tenant_id,
        party_type=PartyType.person,
        display_name=display_name,
        email=email,
        is_active=True,
    )
    setup.add(party)
    setup.flush()
    setup.add(PartyPerson(party_id=party.id, first_name=first_name, last_name=last_name))
    setup.flush()
    setup.add(
        AcademyAccountProfile(
            tenant_id=tenant_id, party_id=party.id, status="active", prefs={}, created_at=now, updated_at=now
        )
    )
    credential = UserCredential(tenant_id=tenant_id, party_id=party.id, password_hash=hash_password(password))
    setup.add(credential)
    setup.flush()
    setup.add(
        AcademyCredentialPolicy(
            tenant_id=tenant_id,
            credential_id=credential.id,
            email=party.email,
            failed_login_attempts=0,
            locked_until=None,
            lifecycle_version=lifecycle_version,
            reset_required=False,
            created_at=now,
            updated_at=now,
        )
    )
    setup.commit()
    return party, credential


@pytest.fixture
def sign_in_account(admin_session, tenant_a, app_user_session):
    party, credential = _canonical_party(
        admin_session,
        tenant_a.id,
        display_name="Sign In",
        first_name="Sign",
        last_name="In",
        email=f"signin-{uuid4().hex}@a.edu",
        password="chosen-password9",
    )
    db = app_user_session
    assert db.get_bind().dialect.name == "postgresql"
    assert db.scalar(text("SELECT current_user")) == "app_user"
    db.execute(text("SELECT set_config('app.current_tenant', :tenant, true)"), {"tenant": str(tenant_a.id)})
    return db, tenant_a.id, party.id, party.email, credential.id


@pytest.fixture
def current_account(request):
    from app.services.credential_authentication import sign_in

    request.getfixturevalue("app_client")
    db, tid, pid, email, cid = request.getfixturevalue("sign_in_account")
    result = sign_in(db, tenant_id=tid, email=email, password="chosen-password9")
    assert result is not None
    db.commit()  # Test setup ends the login writer lock before independent changes.
    resolve_database_runtime().set_tenant(db, tid)
    assert db.scalar(text("SELECT current_user")) == "app_user"
    return SimpleNamespace(
        db=db,
        admin=request.getfixturevalue("admin_session"),
        tenant=request.getfixturevalue("tenant_a"),
        tid=tid,
        pid=pid,
        email=email,
        credential_id=cid,
        token=result.session_token,
        session_id=result.session_id,
        login_snapshot=result.account,
    )


@pytest.fixture
def canonical_account(admin_session, tenant_a, app_user_session, app_client):
    # app_client imports app.main: the genuine installed audit registry
    # and kernel runtime the credential-lifecycle owner requires.
    assert app_client
    party, credential = _canonical_party(
        admin_session,
        tenant_a.id,
        display_name="Canonical Lifecycle",
        first_name="Canonical",
        last_name="Lifecycle",
        email=f"lifecycle-{uuid4().hex}@a.edu",
        password="origpass1",
        lifecycle_version=2,
    )
    db = app_user_session
    assert db.get_bind().dialect.name == "postgresql"
    assert db.scalar(text("SELECT current_user")) == "app_user"
    db.execute(text("SELECT set_config('app.current_tenant', :tenant, true)"), {"tenant": str(tenant_a.id)})
    return db, tenant_a.id, party.id, party.email, credential.id


@pytest.fixture
def invitation_account(request):
    from app.models.account_token import AccountToken
    from app.services.credential_policy import AcademyPasswordPolicy

    now = datetime(2026, 10, 18, 12, tzinfo=UTC)
    request.getfixturevalue("app_client")
    tenant = request.getfixturevalue("tenant_a")
    admin = request.getfixturevalue("admin_session")
    db = request.getfixturevalue("app_user_session")
    assert db.get_bind().dialect.name == "postgresql"
    assert not db.execute(text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user")).scalar_one()
    fks = inspect(admin.get_bind()).get_foreign_keys("account_tokens", schema="public")
    assert any(
        fk["referred_table"] == "parties" and fk["constrained_columns"] == ["tenant_id", "person_id"] for fk in fks
    )
    assert not any(fk["referred_table"] == "people" for fk in fks), "legacy token FK remains unresolved"

    resolve_database_runtime().set_tenant(db, tenant.id)
    party = Party(
        id=uuid4(),
        tenant_id=tenant.id,
        party_type=PartyType.person,
        display_name="Invitation",
        email=f"Invite-{uuid4().hex}@a.edu",
        is_active=False,
        created_at=now,
        updated_at=now,
    )
    db.add(party)
    db.flush()
    db.add(PartyPerson(party_id=party.id, first_name="Invitation", last_name="Subject"))
    db.flush()
    profile = AcademyAccountProfile(
        tenant_id=tenant.id, party_id=party.id, status="suspended", prefs={}, created_at=now, updated_at=now
    )
    raw = uuid4().hex + uuid4().hex
    token = AccountToken(
        tenant_id=tenant.id, person_id=party.id, kind="invite", token_hash=hash_token(raw), expires_at=now
    )
    from datetime import timedelta

    sibling = AccountToken(
        tenant_id=tenant.id,
        person_id=party.id,
        kind="invite",
        token_hash=hash_token(uuid4().hex),
        expires_at=now - timedelta(days=1),
    )
    db.add_all([profile, token, sibling])
    db.flush()
    return db, tenant.id, party, profile, raw, token, sibling, AcademyPasswordPolicy(minimum_length=8)
