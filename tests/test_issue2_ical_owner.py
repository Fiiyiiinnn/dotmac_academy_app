"""Maintained port of legacy_probe/services/test_ical_owner.py (issue #2 wave 5).

Also hosts the wave-5 canonical-account fixture chain (sign_in_account /
current_account) adapted to the maintained per-test ``genesis_database``
fixture; sibling wave-5 modules import fixtures/helpers from here, mirroring
the legacy cross-module fixture imports. PlatformSetting fixtures are ported
to kernel DomainSetting via app/services/settings_store.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from time import monotonic, sleep
from types import SimpleNamespace
from urllib.parse import quote
from uuid import uuid4

import pytest
from dotmac_kernel.models import Party, PartyPerson, PartyRoleGrant, PartyType, Role, Tenant, UserCredential
from dotmac_kernel.session_runtime import resolve_database_runtime
from dotmac_kernel.settings_models import DomainSetting
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import Request

from app.models.identity_extensions import AcademyAccountProfile, AcademyCredentialPolicy
from app.services import settings_store
from app.services.credential_authentication import sign_in
from app.services.security import hash_password


def _request(tenant, token=None):
    headers = [] if token is None else [(b"cookie", f"session={token}".encode())]
    return Request({"type": "http", "headers": headers, "state": {"tenant": tenant}})


def _bind(session, tenant_id):
    assert session.scalar(text("SELECT current_user")) == "app_user"
    resolve_database_runtime().set_tenant(session, tenant_id)


def _wait_for_lock(observer, pid):
    deadline = monotonic() + 10
    while not observer.scalar(
        text("SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = :pid"), {"pid": pid}
    ):
        assert monotonic() < deadline, "writer never reached the genuine row lock wait"
        sleep(0.01)


def login_location(path: str) -> str:
    return "/login?next=" + quote(path, safe="")


def grant_role(db, *, tenant_id, party_id, role: str = "student"):
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


def _grant(a, slug, tenant_id=None):
    if tenant_id is None and slug not in {"student", "instructor", "admin"}:
        grant_role(a.admin, tenant_id=a.tid, party_id=a.pid)
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


_POLICY_KEYS = (
    "admin_home_access_roles",
    "lab_admin_access_roles",
    "reminders_admin_access_roles",
    "gradebook_access_roles",
)


def _restore_keys(a, keys):
    original = settings_store.get_all(a.admin)
    try:
        yield original
    finally:
        a.db.rollback()
        a.admin.rollback()
        for key in keys:
            if key in original:
                settings_store.set_many(a.admin, {key: original[key]})
            else:
                settings_store.delete(a.admin, key)
        a.admin.commit()


@pytest.fixture(autouse=True)
def _restore_policy(request):
    if "current_account" not in request.fixturenames:
        yield
        return
    a = request.getfixturevalue("current_account")
    yield from _restore_keys(a, _POLICY_KEYS)


@pytest.fixture(autouse=True)
def _restore_settings(request):
    if "current_account" not in request.fixturenames:
        yield
        return
    a = request.getfixturevalue("current_account")
    original = settings_store.get_all(a.admin)
    try:
        yield
    finally:
        a.db.rollback()
        a.admin.rollback()
        current = settings_store.get_all(a.admin)
        for key in current:
            if key not in original:
                settings_store.delete(a.admin, key)
        for key, value in original.items():
            settings_store.set_many(a.admin, {key: value})
        a.admin.commit()


def _allow(a, key=None):
    slug = f"purpose-{uuid4().hex}"
    grant = _grant(a, slug)
    keys = _POLICY_KEYS if key is None else (key, *_POLICY_KEYS)
    settings_store.set_many(a.admin, {k: json.dumps([slug]) for k in dict.fromkeys(keys)})
    a.admin.commit()
    return grant, slug


def _client(a):
    from app.api.deps import get_db
    from app.web import admin_home, lab_admin, reminders_admin

    app = FastAPI()

    @app.middleware("http")
    async def tenant(request, call_next):
        request.state.tenant = a.tenant
        return await call_next(request)

    app.dependency_overrides[get_db] = lambda: a.db
    for module in (admin_home, lab_admin, reminders_admin):
        app.include_router(module.router)
    client = TestClient(app, follow_redirects=False)
    client.cookies.set("session", a.token)
    return client


def _party(a, tenant_id):
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


def _log(a):
    from app.models.reminder import ReminderLog

    log = ReminderLog(
        tenant_id=a.tid,
        person_id=a.pid,
        event_kind="graded",
        occurrence_key=f"hosted:{uuid4()}",
        title="Private <grade>",
        link="/progress",
        status="queued",
    )
    a.admin.add(log)
    a.admin.commit()
    return log.id


def make_person_party(db, *, tenant_id, email, first_name, last_name, status="active", prefs=None, avatar_path=None):
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


def _report_person(db, tid, email, first_name, last_name):
    p = Party(
        tenant_id=tid,
        party_type=PartyType.person,
        display_name=f"{first_name} {last_name}".strip(),
        email=email,
        is_active=True,
    )
    db.add(p)
    db.flush()
    db.add(PartyPerson(party_id=p.id, first_name=first_name, last_name=last_name))
    db.flush()
    return p


def seed_report_cohort(db, tid):
    from app.models.assessment import Activity, Score, Submission
    from app.models.cohort import Cohort, Enrollment
    from app.models.course import Course
    from app.models.offering import CourseOffering

    c = Course(
        tenant_id=tid,
        slug="net",
        title="Networking",
        discipline="networking",
        source_ref="x",
        version=1,
        status="published",
        listed=False,
    )
    db.add(c)
    db.flush()
    a1 = Activity(
        tenant_id=tid,
        course_id=c.id,
        chapter_number=1,
        type="mcq_test",
        title="Ch1 Test",
        pass_threshold=0.6,
        grading="auto",
        weight=1.0,
    )
    a2 = Activity(
        tenant_id=tid,
        course_id=c.id,
        chapter_number=2,
        type="lab",
        title="Ch2 Lab",
        pass_threshold=0.6,
        grading="auto",
        weight=1.0,
    )
    db.add_all([a1, a2])
    db.flush()
    coh = Cohort(tenant_id=tid, name="Abuja 2026", discipline="networking", status="active")
    db.add(coh)
    db.flush()
    stu_a = _report_person(db, tid, email="a@stu.edu", first_name="Aaa", last_name="Student")
    stu_b = _report_person(db, tid, email="b@stu.edu", first_name="Bbb", last_name="Student")
    db.add_all([stu_a, stu_b])
    db.flush()
    for p in (stu_a, stu_b):
        db.add(Enrollment(tenant_id=tid, cohort_id=coh.id, person_id=p.id, role_in_cohort="student", status="active"))
    db.add(CourseOffering(tenant_id=tid, cohort_id=coh.id, course_id=c.id, status="active"))
    db.flush()

    def _score(person, activity, frac, passed):
        sub = Submission(tenant_id=tid, activity_id=activity.id, person_id=person.id, answers={}, attempt_no=1)
        db.add(sub)
        db.flush()
        db.add(
            Score(
                tenant_id=tid,
                submission_id=sub.id,
                score=frac * 10,
                max_score=10,
                fraction=frac,
                passed=passed,
                per_item=[],
                source="auto",
            )
        )
        db.flush()

    _score(stu_a, a1, 1.0, True)
    _score(stu_a, a2, 0.4, False)
    _score(stu_b, a1, 1.0, True)
    _score(stu_b, a2, 0.8, True)
    return c, coh, a1, a2, stu_a, stu_b


def _seed(a, tid=None):
    """Catalog-gradebook seed (legacy web/test_catalog_gradebook_owner._seed)."""
    from app.models.assessment import Activity, Score, Submission
    from app.models.cohort import Cohort, Enrollment
    from app.models.course import Course
    from app.models.offering import CourseOffering

    tid = tid or a.tid
    course = Course(
        tenant_id=tid,
        slug=f"gb-{uuid4().hex}",
        title="Owner Gradebook",
        discipline="networking",
        source_ref="hosted-test",
        version=1,
        listed=True,
        status="published",
    )
    a.admin.add(course)
    a.admin.flush()
    activities = [
        Activity(
            tenant_id=tid,
            course_id=course.id,
            chapter_number=i + 1,
            type="mcq_test",
            title=f"Quiz {i}",
            pass_threshold=0.6,
            weight=w,
            grading="auto",
        )
        for i, w in enumerate([1.0, 3.0])
    ]
    cohort = Cohort(tenant_id=tid, name=f"Owner {uuid4().hex}", discipline="networking", status="active")
    a.admin.add_all([*activities, cohort])
    a.admin.flush()
    a.admin.add(CourseOffering(tenant_id=tid, cohort_id=cohort.id, course_id=course.id, status="active"))
    people = [_party(a, tid), _party(a, tid)]
    for person in people:
        a.admin.add(
            Enrollment(
                tenant_id=tid, cohort_id=cohort.id, person_id=person.id, role_in_cohort="student", status="active"
            )
        )
    a.admin.flush()
    sub = Submission(tenant_id=tid, activity_id=activities[0].id, person_id=people[0].id, answers={}, attempt_no=1)
    a.admin.add(sub)
    a.admin.flush()
    a.admin.add(
        Score(
            tenant_id=tid,
            submission_id=sub.id,
            score=10,
            max_score=10,
            fraction=1.0,
            passed=True,
            per_item=[],
            source="auto",
        )
    )
    a.admin.commit()
    return course, cohort, people


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


@pytest.fixture
def admin_session(genesis_database):
    admin, _superuser, _name = genesis_database
    with Session(admin) as db:
        yield db
        db.rollback()


@pytest.fixture
def app_user_session(genesis_database):
    _admin, superuser, name = genesis_database
    url = make_url(superuser).set(database=name, username="app_user", password=None)
    engine = create_engine(url.render_as_string(hide_password=False), future=True)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    db = factory()
    try:
        yield db
        db.rollback()
    finally:
        db.close()
        engine.dispose()


def _make_tenant(admin_session, slug, name):
    admin_session.rollback()
    admin_session.execute(text("DELETE FROM tenants WHERE slug = :s"), {"s": slug})
    admin_session.commit()
    t = Tenant(slug=slug, name=name)
    admin_session.add(t)
    admin_session.commit()
    admin_session.refresh(t)
    return t


@pytest.fixture
def tenant_a(admin_session):
    t = _make_tenant(admin_session, "alpha", "Alpha Test Tenant")
    yield t
    admin_session.rollback()
    admin_session.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": str(t.id)})
    admin_session.commit()


@pytest.fixture
def tenant_b(admin_session):
    t = _make_tenant(admin_session, "beta", "Beta Test Tenant")
    yield t
    admin_session.rollback()
    admin_session.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": str(t.id)})
    admin_session.commit()


@pytest.fixture
def app_client():
    from app.main import app

    return TestClient(app)


@pytest.fixture
def sign_in_account(admin_session, tenant_a, app_user_session):
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


@pytest.fixture
def current_account(request):
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


from concurrent.futures import ThreadPoolExecutor  # noqa: E402
from datetime import timedelta  # noqa: E402
from threading import Event  # noqa: E402

from sqlalchemy import update  # noqa: E402

from app.config import Settings  # noqa: E402
from app.models.account_token import AccountToken  # noqa: E402
from app.services import ical  # noqa: E402
from app.services.security import hash_token  # noqa: E402


def create_roster_actor(a, role_slug):
    """A separate active principal (legacy roster_request_fixtures)."""
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


POLICY = ("ical_feed_token_ttl_days", "ical_feed_uid_domain", "ical_feed_filename", "branding_name")


@pytest.fixture
def calendar_account(current_account):
    a = current_account
    saved = settings_store.get_all(a.admin)
    try:
        yield a
    finally:
        a.db.rollback()
        a.admin.rollback()
        for key in POLICY:
            if key in saved:
                settings_store.set_many(a.admin, {key: saved[key]})
            else:
                settings_store.delete(a.admin, key)
        a.admin.commit()


def test_rotation_multiuse_used_expired_tenant_and_rollback(calendar_account):
    a = calendar_account
    now = datetime.now(UTC)
    root = a.db.get_transaction()
    raw = ical.mint_feed_token(a.db, tenant_id=a.tid, person_id=a.pid, now=now)
    stored = a.db.scalars(select(AccountToken).where(AccountToken.token_hash == hash_token(raw))).one()
    assert raw != stored.token_hash and len(stored.token_hash) == 64
    assert stored.used_at is None
    assert ical.has_feed_token(a.db, tenant_id=a.tid, person_id=a.pid, now=now)
    for _ in range(2):
        assert ical.resolve_feed_token(a.db, tenant_id=a.tid, raw=raw, now=now) == a.pid
    assert ical.resolve_feed_token(a.db, tenant_id=uuid4(), raw=raw, now=now) is None
    assert ical.resolve_feed_token(a.db, tenant_id=a.tid, raw="", now=now) is None
    assert ical.resolve_feed_token(a.db, tenant_id=a.tid, raw=raw, now=stored.expires_at) is None
    stored.used_at = now
    a.db.flush()
    assert ical.resolve_feed_token(a.db, tenant_id=a.tid, raw=raw, now=now) is None
    assert not ical.has_feed_token(a.db, tenant_id=a.tid, person_id=a.pid, now=now)
    stored.used_at = None
    a.db.flush()
    replacement = ical.mint_feed_token(a.db, tenant_id=a.tid, person_id=a.pid, now=now)
    assert ical.resolve_feed_token(a.db, tenant_id=a.tid, raw=raw, now=now) is None
    assert ical.resolve_feed_token(a.db, tenant_id=a.tid, raw=replacement, now=now) == a.pid
    assert a.db.get_transaction() is root
    a.db.rollback()
    assert ical.resolve_feed_token(a.db, tenant_id=a.tid, raw=replacement, now=now) is None


def test_scalar_token_read_observes_remote_invalidation_and_rotation_rollback(calendar_account):
    a = calendar_account
    raw = ical.mint_feed_token(a.db, tenant_id=a.tid, person_id=a.pid)
    a.db.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    retained = a.db.scalars(select(AccountToken).where(AccountToken.token_hash == hash_token(raw))).one()
    assert retained.used_at is None
    a.admin.execute(update(AccountToken).where(AccountToken.id == retained.id).values(used_at=datetime.now(UTC)))
    a.admin.commit()
    assert retained.used_at is None  # Deliberately retained stale ORM observation.
    assert ical.resolve_feed_token(a.db, tenant_id=a.tid, raw=raw) is None
    assert not ical.has_feed_token(a.db, tenant_id=a.tid, person_id=a.pid)
    a.admin.execute(update(AccountToken).where(AccountToken.id == retained.id).values(used_at=None))
    a.admin.commit()
    replacement = ical.mint_feed_token(a.db, tenant_id=a.tid, person_id=a.pid)
    assert ical.resolve_feed_token(a.db, tenant_id=a.tid, raw=raw) is None
    a.db.rollback()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert ical.resolve_feed_token(a.db, tenant_id=a.tid, raw=raw) == a.pid
    assert ical.resolve_feed_token(a.db, tenant_id=a.tid, raw=replacement) is None


def test_concurrent_rotations_wait_on_actual_person_and_only_last_survives(calendar_account):
    a = calendar_account
    a.db.commit()
    factory = sessionmaker(bind=a.db.get_bind(), autoflush=False)
    locked, started, release = Event(), Event(), Event()
    backend, tokens = [], {}

    def leader():
        with factory() as db:
            _bind(db, a.tid)
            tokens["first"] = ical.mint_feed_token(db, tenant_id=a.tid, person_id=a.pid)
            locked.set()
            assert release.wait(10)
            db.commit()

    def follower():
        with factory() as db:
            _bind(db, a.tid)
            backend.append(db.scalar(text("SELECT pg_backend_pid()")))
            started.set()
            tokens["second"] = ical.mint_feed_token(db, tenant_id=a.tid, person_id=a.pid)
            db.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(leader)
        assert locked.wait(10)
        second = pool.submit(follower)
        assert started.wait(10)
        try:
            resolve_database_runtime().set_tenant(a.db, a.tid)
            _wait_for_lock(a.db, backend[0])
        finally:
            release.set()
        first.result(timeout=20)
        second.result(timeout=20)
    assert ical.resolve_feed_token(a.db, tenant_id=a.tid, raw=tokens["first"]) is None
    assert ical.resolve_feed_token(a.db, tenant_id=a.tid, raw=tokens["second"]) == a.pid


def test_custom_ttl_and_strict_store_policy(calendar_account):
    a = calendar_account
    settings_store.set_many(a.admin, {"ical_feed_token_ttl_days": "12"})
    a.admin.commit()
    now = datetime.now(UTC)
    raw = ical.mint_feed_token(a.db, tenant_id=a.tid, person_id=a.pid, now=now)
    expiry = a.db.scalar(select(AccountToken.expires_at).where(AccountToken.token_hash == hash_token(raw)))
    assert expiry == now + timedelta(days=12)
    for value in (True, False, 0, -1, 1.5, "bad", None):
        with pytest.raises(ValueError):
            Settings(ical_feed_token_ttl_days=value)
    for key, value in (
        ("ical_feed_token_ttl_days", "0"),
        ("ical_feed_uid_domain", "x\rINJECT"),
        ("ical_feed_filename", 'bad".ics'),
        ("ical_feed_filename", "../bad.ics"),
    ):
        with pytest.raises(ValueError):
            settings_store.set_many(a.db, {key: value})
    from sqlalchemy.exc import IntegrityError

    a.admin.add(
        DomainSetting(
            tenant_id=None,
            scope_kind="platform",
            domain="academy",
            key="ical_feed_uid_domain",
            value_text=None,
            value_json=None,
        )
    )
    with pytest.raises(IntegrityError):
        a.admin.flush()
    a.admin.rollback()


@pytest.mark.parametrize("line", ["a" * 74 + "🌍" * 80, "学" * 100, "e\u0301" * 100, "plain", ""])
def test_rfc_fold_roundtrip_all_unicode_and_octet_limit(line):
    folded = ical._fold(line)
    assert folded.replace("\r\n ", "") == line
    assert all(len(part.encode("utf-8")) <= 75 for part in folded.split("\r\n"))


def test_text_injection_uri_and_rfc_render():
    now = datetime(2026, 10, 19, tzinfo=UTC)
    body = ical.render_feed(
        [
            {
                "uid": "stable@domain",
                "summary": "学" * 90 + "\rINJECT\nEND",
                "starts_at": now,
                "url": "https://example.test/a,b;c\r\nINJECT",
            }
        ],
        calname="Name\r\nLine",
        now=now,
    )
    unfolded = body.replace("\r\n ", "")
    assert "SUMMARY:" + "学" * 90 + "\\nINJECT\\nEND\r\n" in unfolded
    assert "X-WR-CALNAME:Name\\nLine\r\n" in unfolded
    assert "URL:https://example.test/a,b;cINJECT\r\n" in unfolded
    assert all(len(line.encode("utf-8")) <= 75 for line in body.split("\r\n"))
    assert "\r\nINJECT" not in body
