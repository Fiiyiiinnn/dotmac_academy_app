"""Shared support for the issue #2 wave-2 maintained ports.

Two layers:

* OUTER: ``run_inner`` spawns a pytest subprocess (the
  ``tests/test_boot_smoke.py`` pattern) whose product settings and
  sealed kernel database runtime bind to the per-test
  ``genesis_database`` exactly as a deployment would. The kernel
  runtime binding is one-per-process and cannot follow per-test
  genesis databases inside this process, so runtime-bound legacy
  canaries (bootstrap, sign-in-backed route reads, assessment submit)
  run as INNER tests in that subprocess.

* INNER (``WAVE2_INNER=1`` subprocess only): the genuine legacy
  fixture chain, copied from ``legacy_probe/conftest.py``,
  ``legacy_probe/services/test_canonical_sign_in.py``,
  ``legacy_probe/services/test_canonical_current_user.py``,
  ``legacy_probe/services/roster_request_fixtures.py``,
  ``legacy_probe/services/test_genuine_human_admissions_context.py``
  (``admission_case``), ``legacy_probe/services/test_entrance_exam.py``
  (``_bank_with_questions``),
  ``legacy_probe/services/test_canonical_self_profile_locking.py``
  (``_wait_for_lock``) and
  ``legacy_probe/web/test_reports_access_policy.py``
  (``_nominate_cohort``), adapted to env-provided per-role DSNs for
  the genesis database. All ``app`` imports are lazy so importing
  this module in the OUTER process binds nothing.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic, sleep
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]

OFFLINE_ROLE_PAIR = ("platform_api", "platform_api")


def _url_for(base: str, dbname: str, user: str) -> str:
    return make_url(base).set(database=dbname, username=user, password=None).render_as_string(hide_password=False)


def run_inner(genesis_database, test_relpath: str, timeout: int = 1200):
    """Run one maintained file's INNER tests against the genesis DB."""
    _admin, superuser, name = genesis_database
    env = {
        **os.environ,
        "DATABASE_URL": _url_for(superuser, name, "app_user"),
        "PLATFORM_DATABASE_URL": _url_for(superuser, name, "platform_api"),
        "TEST_DATABASE_URL": _url_for(superuser, name, "app_user"),
        "TEST_MIGRATION_DATABASE_URL": _url_for(superuser, name, "app_admin"),
        "CI": "true",
        "ENVIRONMENT": "dev",
        "WAVE2_INNER": "1",
        "WAVE2_DBNAME": name,
    }
    return subprocess.run(  # noqa: S603 - fixed interpreter + repo test path
        [sys.executable, "-m", "pytest", test_relpath, "-q", "-p", "no:cacheprovider"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def assert_inner_passed(result) -> None:
    output = result.stdout[-6000:] + result.stderr[-3000:]
    assert " passed" in result.stdout, output
    assert result.returncode == 0, output


# ---------------------------------------------------------------------------
# INNER fixture chain (only exercised inside the WAVE2_INNER subprocess)
# ---------------------------------------------------------------------------

_bound = False


def _bind_runtime() -> None:
    global _bound
    if not _bound:
        from app.database_runtime import bind_academy_database_runtime

        bind_academy_database_runtime()
        _bound = True


@pytest.fixture
def admin_session():
    _bind_runtime()
    engine = create_engine(os.environ["TEST_MIGRATION_DATABASE_URL"], future=True)
    db = sessionmaker(bind=engine, autocommit=False, autoflush=False)()
    try:
        yield db
        db.rollback()
    finally:
        db.close()
        engine.dispose()


@pytest.fixture
def app_user_session():
    _bind_runtime()
    engine = create_engine(os.environ["TEST_DATABASE_URL"], future=True)
    db = sessionmaker(bind=engine, autocommit=False, autoflush=False)()
    try:
        yield db
        db.rollback()
    finally:
        db.close()
        engine.dispose()


def _make_tenant(admin_session, slug: str, name: str):
    from app.models.tenant import Tenant

    admin_session.rollback()
    admin_session.execute(text("DELETE FROM tenants WHERE slug = :s"), {"s": slug})
    admin_session.commit()
    t = Tenant(slug=slug, name=name)
    admin_session.add(t)
    admin_session.commit()
    admin_session.refresh(t)
    return t


def _drop_tenant(admin_session, t) -> None:
    admin_session.rollback()
    admin_session.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": str(t.id)})
    admin_session.commit()


@pytest.fixture
def tenant_a(admin_session):
    t = _make_tenant(admin_session, "alpha", "Alpha Test Tenant")
    yield t
    _drop_tenant(admin_session, t)


@pytest.fixture
def tenant_b(admin_session):
    t = _make_tenant(admin_session, "beta", "Beta Test Tenant")
    yield t
    _drop_tenant(admin_session, t)


@pytest.fixture
def sign_in_account(admin_session, tenant_a, app_user_session):
    from dotmac_kernel.models import Party, PartyPerson, PartyType, UserCredential

    from app.models.identity_extensions import AcademyAccountProfile, AcademyCredentialPolicy
    from app.services.security import hash_password

    setup = admin_session
    now = datetime.now(UTC)
    party = Party(
        tenant_id=tenant_a.id,
        party_type=PartyType.person,
        display_name="Sign In",
        email=f"signin-{uuid4().hex}@a.edu",
        is_active=True,
    )
    setup.add(party)
    setup.flush()
    setup.add(PartyPerson(party_id=party.id, first_name="Sign", last_name="In"))
    setup.flush()
    setup.add(
        AcademyAccountProfile(
            tenant_id=tenant_a.id, party_id=party.id, status="active", prefs={}, created_at=now, updated_at=now
        )
    )
    credential = UserCredential(
        tenant_id=tenant_a.id, party_id=party.id, password_hash=hash_password("chosen-password9")
    )
    setup.add(credential)
    setup.flush()
    setup.add(
        AcademyCredentialPolicy(
            tenant_id=tenant_a.id,
            credential_id=credential.id,
            email=party.email,
            failed_login_attempts=0,
            locked_until=None,
            lifecycle_version=0,
            reset_required=False,
            created_at=now,
            updated_at=now,
        )
    )
    setup.commit()
    db = app_user_session
    assert db.get_bind().dialect.name == "postgresql"
    assert db.scalar(text("SELECT current_user")) == "app_user"
    db.execute(text("SELECT set_config('app.current_tenant', :tenant, true)"), {"tenant": str(tenant_a.id)})
    return db, tenant_a.id, party.id, party.email, credential.id


def _request(tenant, token=None):
    from starlette.requests import Request

    headers = [] if token is None else [(b"cookie", f"session={token}".encode())]
    return Request({"type": "http", "headers": headers, "state": {"tenant": tenant}})


@pytest.fixture
def current_account(request):
    from dotmac_kernel.session_runtime import resolve_database_runtime

    from app.services.credential_authentication import sign_in

    assert os.environ.get("CI") == "true", "canonical guard canaries are hosted only"
    assert os.getenv("TEST_DATABASE_URL"), "canonical app_user DSN is required"
    assert os.getenv("TEST_MIGRATION_DATABASE_URL"), "canonical migration DSN is required"
    _bind_runtime()
    db, tid, pid, email, cid = request.getfixturevalue("sign_in_account")
    result = sign_in(db, tenant_id=tid, email=email, password="chosen-password9")
    assert result is not None
    db.commit()
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


def create_roster_actor(a, role_slug):
    from dotmac_kernel.models import Party, PartyPerson, PartyRoleGrant, PartyType, Role, UserCredential
    from dotmac_kernel.session_runtime import resolve_database_runtime
    from sqlalchemy import select

    from app.models.identity_extensions import AcademyAccountProfile, AcademyCredentialPolicy
    from app.services.credential_authentication import sign_in
    from app.services.security import hash_password

    a.db.rollback()
    now = datetime.now(UTC)
    person = Party(
        tenant_id=a.tid,
        party_type=PartyType.person,
        display_name="Roster Actor",
        email=f"roster-{uuid4().hex}@a.edu",
        is_active=True,
    )
    a.admin.add(person)
    a.admin.flush()
    a.admin.add(PartyPerson(party_id=person.id, first_name="Roster", last_name="Actor"))
    a.admin.flush()
    a.admin.add(
        AcademyAccountProfile(
            tenant_id=a.tid, party_id=person.id, status="active", prefs={}, created_at=now, updated_at=now
        )
    )
    credential = UserCredential(tenant_id=a.tid, party_id=person.id, password_hash=hash_password("chosen-password9"))
    a.admin.add(credential)
    a.admin.flush()
    a.admin.add(
        AcademyCredentialPolicy(
            tenant_id=a.tid,
            credential_id=credential.id,
            email=person.email,
            failed_login_attempts=0,
            lifecycle_version=0,
            reset_required=False,
            created_at=now,
            updated_at=now,
        )
    )
    role = a.admin.scalars(select(Role).where(Role.tenant_id == a.tid, Role.slug == role_slug)).first()
    if role is None:
        role = Role(tenant_id=a.tid, slug=role_slug, name=role_slug)
        a.admin.add(role)
        a.admin.flush()
    a.admin.add(PartyRoleGrant(tenant_id=a.tid, party_id=person.id, role_id=role.id))
    a.admin.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    login = sign_in(a.db, tenant_id=a.tid, email=person.email, password="chosen-password9")
    assert login is not None
    a.db.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    return SimpleNamespace(
        request=_request(a.tenant, login.session_token),
        pid=person.id,
        email=person.email,
        session_id=login.session_id,
        role_id=role.id,
    )


@pytest.fixture
def roster_actors(current_account):
    a = current_account
    instructor = create_roster_actor(a, "instructor")
    admin = create_roster_actor(a, "admin")
    return SimpleNamespace(db=a.db, instructor=instructor, admin=admin, account=a)


def _bank_with_questions(admin_session, tenant):
    from app.models.assessment import Question, QuestionBank
    from app.models.course import Course

    course = Course(
        tenant_id=tenant.id,
        slug=f"c-{uuid4().hex[:6]}",
        title="Intake",
        discipline="fiber",
        source_ref="x",
        version=1,
        status="published",
        listed=False,
    )
    admin_session.add(course)
    admin_session.flush()
    bank = QuestionBank(tenant_id=tenant.id, course_id=course.id, chapter_number=1, kind="chapter", version=1)
    admin_session.add(bank)
    admin_session.flush()
    for ext, cat in [("q1", "numeracy"), ("q2", "numeracy"), ("q3", "safety"), ("q4", "safety")]:
        admin_session.add(
            Question(
                tenant_id=tenant.id,
                bank_id=bank.id,
                ext_id=ext,
                stem="?",
                type="single",
                options=["A", "B", "C", "D"],
                correct=["A"],
                rubric_category="recall",
                category=cat,
                explanation="",
                weight=1,
            )
        )
    admin_session.flush()
    return bank


def _admission_request(actors, *, transport="web", tenant=None):
    from starlette.requests import Request

    actor = actors.admin
    headers = list(actor.request.scope["headers"])
    if transport == "api":
        token = actor.request.cookies["session"]
        headers = [(b"authorization", f"Bearer {token}".encode())]
    return Request(
        {
            "type": "http",
            "scheme": "https",
            "server": ("academy.example", 443),
            "path": "/",
            "root_path": "",
            "query_string": b"",
            "headers": headers,
            "state": {"tenant": tenant or actors.account.tenant},
        }
    )


@pytest.fixture
def admission_case(roster_actors):
    from dotmac_kernel.session_runtime import resolve_database_runtime

    from app.models.admissions import Applicant
    from app.models.cohort import Cohort
    from app.models.track import CohortTrack, Track

    actors, a = roster_actors, roster_actors.account
    bank = _bank_with_questions(a.admin, a.tenant)
    cohort = Cohort(
        tenant_id=a.tid, name="Admission cohort", discipline="fiber", status="active", entrance_bank_id=bank.id
    )
    track = Track(tenant_id=a.tid, name="Admission track", slug=uuid4().hex, status="active")
    a.admin.add_all([cohort, track])
    a.admin.flush()
    a.admin.add(CohortTrack(tenant_id=a.tid, cohort_id=cohort.id, track_id=track.id, status="active"))
    a.admin.flush()
    applicant = Applicant(
        tenant_id=a.tid,
        email=f"admission-{uuid4().hex}@a.edu",
        first_name="Applicant",
        last_name="Human",
        status="applied",
        cohort_id=cohort.id,
        track_id=track.id,
        program=track.name,
    )
    a.admin.add(applicant)
    a.admin.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert a.db.scalar(text("SELECT current_user")) == "app_user"
    return SimpleNamespace(
        actors=actors,
        a=a,
        id=applicant.id,
        cohort_id=cohort.id,
        track_id=track.id,
        email=applicant.email,
        web=_admission_request(actors),
        api=_admission_request(actors, transport="api"),
    )


def _nominate_cohort(a, cohort_id=None):
    from app.models.cohort import Cohort
    from app.models.facilitator import FacilitatorNomination

    if cohort_id is None:
        cohort = Cohort(tenant_id=a.tid, name=f"Reports {uuid4().hex}", discipline="networking", status="active")
        a.admin.add(cohort)
        a.admin.flush()
        cohort_id = cohort.id
    nomination = FacilitatorNomination(
        tenant_id=a.tid,
        person_id=a.pid,
        scope_type="cohort",
        cohort_id=cohort_id,
        status="active",
        activated_at=datetime.now(UTC),
    )
    a.admin.add(nomination)
    a.admin.commit()
    return nomination.id, cohort_id


def _wait_for_lock(observer, pid):
    deadline = monotonic() + 10
    while not observer.scalar(
        text("SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = :pid"), {"pid": pid}
    ):
        assert monotonic() < deadline, "writer never reached the genuine row lock wait"
        sleep(0.01)


@pytest.fixture
def offline_runtime(monkeypatch):
    """Genuine platform runtime for bootstrap canaries (inner subprocess).

    Adapted from legacy_probe/services/test_bootstrap_identity.py: the
    dedicated database is this run's genesis database (WAVE2_DBNAME),
    reached as the real platform_api role pair via PLATFORM_DATABASE_URL.
    """
    from app.config import settings
    from app.database_runtime import bind_academy_database_runtime

    monkeypatch.setattr(settings, "bootstrap_allowed_database_role_pairs", (OFFLINE_ROLE_PAIR,))
    runtime = bind_academy_database_runtime()
    global _bound
    _bound = True
    with runtime.platform_session() as db:
        assert db.get_bind().dialect.name == "postgresql"
        assert db.scalar(text("SELECT current_database()")) == os.environ["WAVE2_DBNAME"]
        pair = db.execute(text("SELECT session_user, current_user")).one()
        from app.services.settings_store import effective

        configured = effective(db, require_store=True)
        assert tuple(pair) in configured.bootstrap_allowed_database_role_pairs
    return runtime
