"""Authored hosted canaries; requires the genuine composed app_user assembly.

Learning V3 owns configured nonstudent creation authorization. These tests call
that actual owner through actual Request boundaries; no fake authority context.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Event
from uuid import uuid4

import pytest
from dotmac_kernel.models import AuthSession, Party, PartyRoleGrant
from dotmac_kernel.session_runtime import resolve_database_runtime
from fastapi import HTTPException
from sqlalchemy import delete, select, text, update
from sqlalchemy.orm import sessionmaker

from app.models.cohort import Cohort, Enrollment
from app.models.course import Course
from app.models.offering import CourseOffering
from app.services import roster, settings_store
from app.services.audit_context import HumanOperationError
from app.web.instructor import _assigned_course_ids
from legacy_probe.services.roster_request_fixtures import roster_actors
from legacy_probe.services.test_canonical_self_profile_locking import _wait_for_lock

assert roster_actors


@pytest.fixture
def configured_instructor_policy(roster_actors):
    a = roster_actors.account
    keys = ("bulk_enrollment_initial_role", "bulk_enrollment_initial_status")
    previous = settings_store.get_all(a.admin)
    settings_store.set_many(a.admin, dict(zip(keys, ("instructor", "active"), strict=True)))
    a.admin.commit()
    try:
        yield
    finally:
        a.db.rollback()
        for key in keys:
            if key in previous:
                settings_store.set_many(a.admin, {key: previous[key]})
            else:
                settings_store.delete(a.admin, key)
        a.admin.commit()


def _cohort(a):
    a.db.rollback()
    cohort = Cohort(tenant_id=a.tid, name="Genuine roster", discipline="networking", status="active")
    a.admin.add(cohort)
    a.admin.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    return cohort.id


def _bulk(actors, cid, *, admin=False, emails=None, db=None):
    actor = actors.admin if admin else actors.instructor
    return roster.bulk_enroll(
        actors.db if db is None else db,
        request=actor.request,
        tenant_id=actors.account.tid,
        cohort_id=cid,
        emails=[actor.email] if emails is None else emails,
    )


def test_configured_instructor_nonadmin_bulk_self_cannot_gain_cohort_authoring_admin_can(roster_actors, configured_instructor_policy):
    a, actors = roster_actors.account, roster_actors
    cid = _cohort(a)
    course = Course(tenant_id=a.tid, slug=f"roster-{uuid4().hex}", title="Owner guard", discipline="networking",
                    source_ref="manual", version=1, status="published", listed=False)
    a.admin.add(course)
    a.admin.flush()
    a.admin.add(CourseOffering(tenant_id=a.tid, cohort_id=cid, course_id=course.id, status="active"))
    a.admin.add(Enrollment(tenant_id=a.tid, cohort_id=cid, person_id=a.pid,
                           role_in_cohort="student", status="active"))
    a.admin.commit()
    result = _bulk(actors, cid, emails=[actors.instructor.email, a.email, "missing@a.edu"])
    assert result["admin_required"] == [actors.instructor.email]
    assert result["not_found"] == ["missing@a.edu"]
    assert result["enrolled"] == []
    assert result["already_active"] == [a.email]
    assert a.db.scalar(select(Enrollment.id).where(Enrollment.cohort_id == cid,
                                                  Enrollment.person_id == actors.instructor.pid)) is None
    assert course.id not in _assigned_course_ids(a.db, tenant_id=a.tid, instructor_id=actors.instructor.pid)
    result = _bulk(actors, cid, admin=True, emails=[actors.instructor.email])
    assert result["enrolled"] == [actors.instructor.email]
    assert course.id in _assigned_course_ids(a.db, tenant_id=a.tid, instructor_id=actors.instructor.pid)


def test_one_policy_snapshot_after_guard_and_cohort_before_actual_owner(roster_actors, monkeypatch):
    actors, a = roster_actors, roster_actors.account
    cid = _cohort(a)
    order = []
    cohort_exists, policy, owner = roster.learning.instructor_cohort_exists, roster.bulk_enrollment_policy, roster.learning.bulk_enroll

    def cohort_read(*args, **kw):
        order.append("cohort")
        return cohort_exists(*args, **kw)

    def snapshot(db):
        order.append("policy")
        return policy(db)

    def write(*args, **kw):
        order.append("owner")
        return owner(*args, **kw)

    monkeypatch.setattr(roster.learning, "instructor_cohort_exists", cohort_read)
    monkeypatch.setattr(roster, "bulk_enrollment_policy", snapshot)
    monkeypatch.setattr(roster.learning, "bulk_enroll", write)
    assert _bulk(actors, cid)["enrolled"] == [actors.instructor.email]
    assert order == ["cohort", "policy", "owner"]


@pytest.mark.parametrize("failure", ["settings", "database", "revocation", "admin_loss"])
def test_fatal_failure_rolls_back_even_when_caught_then_outer_commit(roster_actors, monkeypatch, failure):
    actors, a = roster_actors, roster_actors.account
    cid = _cohort(a)
    original = roster.learning.bulk_enroll

    if failure == "settings":
        def failed_policy(db):
            raise RuntimeError("required settings read failure")
        monkeypatch.setattr(roster, "bulk_enrollment_policy", failed_policy)
    else:
        def interrupted(db, *args, **kw):
            result = original(db, *args, **kw)
            if failure == "database":
                db.execute(text("SELECT 1 / 0"))
            elif failure == "revocation":
                db.execute(update(AuthSession).where(AuthSession.id == actors.admin.session_id).values(revoked_at=datetime.now(UTC)))
            else:
                db.execute(delete(PartyRoleGrant).where(PartyRoleGrant.party_id == actors.admin.pid,
                                                       PartyRoleGrant.role_id == actors.admin.role_id))
            return result
        monkeypatch.setattr(roster.learning, "bulk_enroll", interrupted)
    error = HumanOperationError if failure in ("settings", "database") else HTTPException
    with pytest.raises(error):
        _bulk(actors, cid, admin=True)
    a.db.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert a.db.scalar(select(Enrollment.id).where(Enrollment.cohort_id == cid)) is None
    assert a.db.get(AuthSession, actors.admin.session_id).revoked_at is None
    assert a.db.scalar(select(PartyRoleGrant.id).where(PartyRoleGrant.party_id == actors.admin.pid,
                                                     PartyRoleGrant.role_id == actors.admin.role_id)) is not None


@pytest.mark.parametrize("change", ["revocation", "role_loss"])
def test_actual_owner_never_called_after_real_party_lock_wait_and_authority_loss(roster_actors, monkeypatch, change):
    actors, a = roster_actors, roster_actors.account
    cid = _cohort(a)
    a.db.commit()
    sessions = sessionmaker(bind=a.db.get_bind(), autoflush=False)
    locked, started, release = Event(), Event(), Event()
    backend = []
    calls = []
    owner = roster.learning.bulk_enroll

    def observe(*args, **kw):
        calls.append(True)
        return owner(*args, **kw)

    monkeypatch.setattr(roster.learning, "bulk_enroll", observe)

    def holder():
        with sessions() as db:
            resolve_database_runtime().set_tenant(db, a.tid)
            db.scalars(select(Party).where(Party.id == actors.instructor.pid).with_for_update()).one()
            if change == "revocation":
                db.execute(update(AuthSession).where(AuthSession.id == actors.instructor.session_id).values(revoked_at=datetime.now(UTC)))
            else:
                db.execute(delete(PartyRoleGrant).where(PartyRoleGrant.party_id == actors.instructor.pid,
                                                       PartyRoleGrant.role_id == actors.instructor.role_id))
            locked.set()
            assert release.wait(10)
            db.commit()

    def follower():
        with sessions() as db:
            assert db.scalar(text("SELECT current_user")) == "app_user"
            resolve_database_runtime().set_tenant(db, a.tid)
            backend.append(db.scalar(text("SELECT pg_backend_pid()")))
            started.set()
            with pytest.raises(HTTPException) as caught:
                _bulk(actors, cid, db=db)
            assert caught.value.status_code == (303 if change == "revocation" else 403)
            db.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(holder)
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
    assert calls == []
    assert a.db.scalar(select(Enrollment.id).where(Enrollment.cohort_id == cid)) is None
