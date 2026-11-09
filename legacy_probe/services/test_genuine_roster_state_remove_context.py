"""Authored hosted-only genuine roster contexts; no local runtime qualification.

The shared actual current_account/sign_in_account chain supplies app_user and
canonical credentials/sessions. Learning remains the only enrollment writer.

C3 (#188) retired the ``remove`` operation variants: facilitator authority is
nomination-based now and ``roster.remove_facilitator`` is deleted, so every
case below exercises the surviving roster-state operation only. The revoke
lifecycle is covered by tests/services/test_facilitator_nominations.py.
"""

from concurrent.futures import ThreadPoolExecutor
from copy import copy
from datetime import UTC, datetime, timedelta
from threading import Event
from uuid import uuid4

import pytest
from dotmac_kernel.audit import AuditEvent
from dotmac_kernel.models import AuthSession, Party, PartyRoleGrant
from dotmac_kernel.session_runtime import resolve_database_runtime
from fastapi import HTTPException
from sqlalchemy import delete, select, text, update
from sqlalchemy.orm import sessionmaker

from app.models.cohort import Cohort, Enrollment
from app.models.identity_extensions import AcademyAccountProfile
from app.services import audit_context as boundary
from app.services import roster
from app.services.exceptions import NotFoundError
from legacy_probe.conftest import web_login
from legacy_probe.services.roster_request_fixtures import roster_actors
from legacy_probe.services.test_canonical_self_profile_locking import _wait_for_lock

assert roster_actors


def _case(actors, *, target_role="student", target_status="active"):
    a = actors.account
    cohort = Cohort(tenant_id=a.tid, name="Before operation", discipline="networking", status="active")
    a.admin.add(cohort)
    a.admin.flush()
    row = None
    if target_role is not None:
        row = Enrollment(
            tenant_id=a.tid, cohort_id=cohort.id, person_id=a.pid, role_in_cohort=target_role, status=target_status
        )
        a.admin.add(row)
    a.admin.commit()
    assert a.db.scalar(text("SELECT current_user")) == "app_user"
    return cohort.id, None if row is None else row.id


def _invoke(actors, operation, cid, *, admin=False, db=None, state="dropped", tenant_id=None):
    actor = actors.admin if admin else actors.instructor
    arguments = dict(
        request=actor.request, tenant_id=tenant_id or actors.account.tid, cohort_id=cid, person_id=actors.account.pid
    )
    assert operation == "state"  # the remove operation retired with #188 (see module docstring)
    return roster.set_roster_state(actors.db if db is None else db, state=state, **arguments)


@pytest.mark.parametrize(
    "operation,admin,role", [("state", False, "student"), ("state", True, "instructor")]
)
@pytest.mark.parametrize("target_condition", ["normal", "inactive", "suspended"])
def test_actual_owner_success_same_session_live_authority_no_invented_audit(
    roster_actors, monkeypatch, operation, admin, role, target_condition
):
    actors, a = roster_actors, roster_actors.account
    cid, eid = _case(actors, target_role=role)
    if target_condition == "inactive":
        a.admin.execute(update(Party).where(Party.id == a.pid).values(is_active=False))
    elif target_condition == "suspended":
        a.admin.execute(
            update(AcademyAccountProfile).where(AcademyAccountProfile.party_id == a.pid).values(status="suspended")
        )
    a.admin.commit()
    owner = roster.learning.set_roster_state
    seen = []

    def observe(db, command, *, authority):
        result = owner(db, command, authority=authority)
        seen.append((db, command, authority, result))
        return result

    monkeypatch.setattr(roster.learning, "set_roster_state", observe)
    root = a.db.get_transaction()
    audits = a.db.scalar(select(text("count(*)")).select_from(AuditEvent))
    result = _invoke(actors, operation, cid, admin=admin)
    assert len(seen) == 1 and result is seen[0][3] and seen[0][0] is a.db
    assert seen[0][1].tenant_id == a.tid and seen[0][1].cohort_id == cid and seen[0][1].person_id == a.pid
    assert seen[0][2].tenant_id == a.tid and seen[0][2].actor_is_admin is admin
    assert result.id == eid and result.status == "dropped"
    assert a.db.get(Enrollment, eid).status == "dropped" and a.db.get_transaction() is root
    assert a.db.scalar(select(text("count(*)")).select_from(AuditEvent)) == audits


@pytest.mark.parametrize("operation", ["state"])
def test_live_instructor_cannot_change_nonstudent_enrollment_state(roster_actors, operation):
    actors, a = roster_actors, roster_actors.account
    cid, eid = _case(actors, target_role="instructor")
    with pytest.raises(HTTPException) as caught:
        _invoke(actors, operation, cid, state="invalid")
    assert caught.value.status_code == 403 and caught.value.detail == "Forbidden"
    a.db.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert a.db.get(Enrollment, eid).status == "active"


@pytest.mark.parametrize(
    "state,detail", [("bogus", "invalid roster state: bogus"), ("dropped", "enrollment not found")]
)
def test_actual_owner_invalid_state_missing_priority_preserved(roster_actors, state, detail):
    actors = roster_actors
    cid, _ = _case(actors, target_role=None)
    with pytest.raises(NotFoundError) as caught:
        _invoke(actors, "state", cid, state=state)
    assert str(caught.value) == detail and caught.value.__suppress_context__ is True


@pytest.mark.parametrize("operation", ["state"])
@pytest.mark.parametrize("change", ["revocation", "expiry", "inactive", "suspended", "role_loss"])
def test_natural_party_wait_rechecks_clock_and_genuine_actor_before_owner(
    roster_actors, monkeypatch, operation, change
):
    actors, a = roster_actors, roster_actors.account
    actor = actors.instructor
    cid, eid = _case(actors, target_role="student")
    a.db.commit()
    sessions = sessionmaker(bind=a.db.get_bind(), autoflush=False)
    locked, started, release = Event(), Event(), Event()
    backend, calls = [], []
    owner = roster.learning.set_roster_state

    def observe(*args, **kwargs):
        calls.append(True)
        return owner(*args, **kwargs)

    monkeypatch.setattr(roster.learning, "set_roster_state", observe)

    def holder():
        with sessions() as db:
            resolve_database_runtime().set_tenant(db, a.tid)
            db.scalar(select(Party).where(Party.id == actor.pid).with_for_update())
            if change == "revocation":
                db.execute(
                    update(AuthSession).where(AuthSession.id == actor.session_id).values(revoked_at=datetime.now(UTC))
                )
            elif change == "expiry":
                db.execute(
                    update(AuthSession)
                    .where(AuthSession.id == actor.session_id)
                    .values(expires_at=datetime.now(UTC) + timedelta(seconds=1))
                )
            elif change == "inactive":
                db.execute(update(Party).where(Party.id == actor.pid).values(is_active=False))
            elif change == "suspended":
                db.execute(
                    update(AcademyAccountProfile)
                    .where(AcademyAccountProfile.party_id == actor.pid)
                    .values(status="suspended")
                )
            else:
                db.execute(
                    delete(PartyRoleGrant).where(
                        PartyRoleGrant.party_id == actor.pid, PartyRoleGrant.role_id == actor.role_id
                    )
                )
            locked.set()
            assert release.wait(10)
            db.commit()

    def follower():
        with sessions() as db:
            resolve_database_runtime().set_tenant(db, a.tid)
            backend.append(db.scalar(text("SELECT pg_backend_pid()")))
            started.set()
            with pytest.raises(HTTPException) as caught:
                _invoke(actors, operation, cid, admin=False, db=db)
            assert caught.value.status_code == (403 if change == "role_loss" else 303)
            db.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(holder)
        assert locked.wait(10)
        second = pool.submit(follower)
        assert started.wait(10)
        try:
            resolve_database_runtime().set_tenant(a.db, a.tid)
            _wait_for_lock(a.db, backend[0])
            if change == "expiry":
                Event().wait(1.1)  # Natural session expiry while genuinely blocked.
        finally:
            release.set()
        first.result(timeout=20)
        second.result(timeout=20)
    assert calls == [] and a.db.get(Enrollment, eid).status == "active"


@pytest.mark.parametrize("operation", ["state"])
@pytest.mark.parametrize("failure", ["owner", "database", "revocation", "role_loss", "admin_to_instructor"])
def test_failure_after_actual_owner_caught_outer_commit_restores_operation_preserves_prior_work(
    roster_actors, monkeypatch, operation, failure
):
    actors, a = roster_actors, roster_actors.account
    cid, eid = _case(actors, target_role="instructor")
    if failure == "admin_to_instructor":
        a.admin.add(PartyRoleGrant(tenant_id=a.tid, party_id=actors.admin.pid, role_id=actors.instructor.role_id))
        a.admin.commit()
    root = a.db.get_transaction()
    a.db.get(Cohort, cid).name = "Unrelated prior work"
    owner = roster.learning.set_roster_state

    def interrupted(db, *args, **kwargs):
        result = owner(db, *args, **kwargs)
        if failure == "owner":
            raise RuntimeError("required Learning owner failure")
        if failure == "database":
            db.execute(text("SELECT 1 / 0"))
        elif failure == "revocation":
            db.execute(
                update(AuthSession)
                .where(AuthSession.id == actors.admin.session_id)
                .values(revoked_at=datetime.now(UTC))
            )
        else:
            db.execute(
                delete(PartyRoleGrant).where(
                    PartyRoleGrant.party_id == actors.admin.pid, PartyRoleGrant.role_id == actors.admin.role_id
                )
            )
        return result

    monkeypatch.setattr(
        roster.learning, "set_roster_state", interrupted
    )
    with pytest.raises(boundary.HumanOperationError if failure in {"owner", "database"} else HTTPException):
        _invoke(actors, operation, cid, admin=True)
    assert a.db.get_transaction() is root
    a.db.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert a.db.get(Cohort, cid).name == "Unrelated prior work"
    assert a.db.get(Enrollment, eid).status == "active"
    assert a.db.get(AuthSession, actors.admin.session_id).revoked_at is None
    assert (
        a.db.scalar(
            select(PartyRoleGrant.id).where(
                PartyRoleGrant.party_id == actors.admin.pid, PartyRoleGrant.role_id == actors.admin.role_id
            )
        )
        is not None
    )
    assert a.db.scalar(text("SELECT current_setting('app.current_tenant')")) == str(a.tid)


@pytest.mark.parametrize("operation", ["state"])
def test_typed_learning_transaction_guard_same_instance_after_real_savepoint_rollback(
    roster_actors, monkeypatch, operation
):
    actors, a = roster_actors, roster_actors.account
    cid, eid = _case(actors, target_role="instructor")
    original = roster.learning.set_roster_state
    failure = roster.learning.TransactionChanged("required owner transaction guard")
    root = a.db.get_transaction()

    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise failure

    monkeypatch.setattr(
        roster.learning, "set_roster_state", interrupted
    )
    with pytest.raises(roster.learning.TransactionChanged) as caught:
        _invoke(actors, operation, cid, admin=True)
    assert caught.value is failure and caught.value.__suppress_context__ is True
    assert a.db.get_transaction() is root
    a.db.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert a.db.get(Enrollment, eid).status == "active"


@pytest.mark.parametrize("operation", ["state"])
@pytest.mark.parametrize("field", ["request", "session", "root", "database", "cohort", "person", "state"])
def test_issued_context_binding_tamper_latches_even_when_caught_and_restored(roster_actors, operation, field):
    actors, a = roster_actors, roster_actors.account
    cid, eid = _case(actors, target_role="instructor")
    issued = boundary.change_web_roster_state(
        a.db, request=actors.admin.request, cohort_id=cid, person_id=a.pid, state="dropped"
    )
    with sessionmaker(bind=a.db.get_bind(), autoflush=False)() as other:
        resolve_database_runtime().set_tenant(other, a.tid)
        with pytest.raises(boundary.HumanOperationError):
            with issued as context:
                if field in {"cohort", "person", "state"}:
                    name = {"cohort": "cohort_id", "person": "party_id", "state": "values"}[field]
                    original = getattr(context.resources, name)
                    object.__setattr__(context.resources, name, ("active",) if field == "state" else uuid4())
                    with pytest.raises(boundary.HumanOperationError):
                        context.revalidate()
                    object.__setattr__(context.resources, name, original)
                else:
                    name = {"request": "request", "session": "session_id", "root": "root", "database": "db"}[field]
                    original = getattr(context, name)
                    replacement = {
                        "request": actors.instructor.request,
                        "session": actors.instructor.session_id,
                        "root": other.get_transaction(),
                        "database": other,
                    }[field]
                    setattr(context, name, replacement)
                    with pytest.raises(boundary.HumanOperationError):
                        context.revalidate()
                    setattr(context, name, original)
                with pytest.raises(boundary.HumanOperationError):
                    context.revalidate()
        assert context.__dict__ == {}
    a.db.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert a.db.get(Enrollment, eid).status == "active"


@pytest.mark.parametrize("operation", ["state"])
def test_copied_context_never_authorizes_and_retained_own_frames_clear_requests(roster_actors, operation):
    actors, a = roster_actors, roster_actors.account
    cid, _ = _case(actors)
    issued = boundary.change_web_roster_state(
        a.db, request=actors.admin.request, cohort_id=cid, person_id=a.pid, state="dropped"
    )
    with issued as context:
        copied = copy(context)
        with pytest.raises(boundary.HumanOperationError) as caught:
            copied.revalidate()
        traceback = caught.value.__traceback__
        while traceback is not None:
            frame = traceback.tb_frame
            if frame.f_code.co_filename == boundary.__file__:
                assert frame.f_locals.get("self") is None
            traceback = traceback.tb_next
    assert context.__dict__ == {}


@pytest.mark.parametrize("operation", ["state"])
def test_wrong_tenant_is_forbidden_without_learning_owner(roster_actors, tenant_b, monkeypatch, operation):
    from unittest.mock import Mock

    actors = roster_actors
    cid, _ = _case(actors)
    owner = Mock()
    monkeypatch.setattr(roster.learning, "set_roster_state", owner)
    with pytest.raises(HTTPException) as caught:
        _invoke(actors, operation, cid, admin=True, tenant_id=tenant_b.id)
    assert caught.value.status_code == 403 and caught.value.detail == "Forbidden"
    owner.assert_not_called()


@pytest.mark.parametrize("admin,htmx", [(False, False), (True, True)])
def test_real_http_web_login_and_state_route_preserve_redirects(roster_actors, app_client, admin, htmx):
    actors, a = roster_actors, roster_actors.account
    cid, eid = _case(actors, target_role="instructor" if admin else "student")
    actor = actors.admin if admin else actors.instructor
    host = {"Host": f"{a.tenant.slug}.localhost"}
    web_login(app_client, host, actor.email, "chosen-password9")
    app_client.get("/instructor/cohorts", headers=host)
    response = app_client.post(
        f"/instructor/cohorts/{cid}/roster/{a.pid}/state",
        headers={
            **host,
            "x-csrf-token": app_client.cookies.get("csrf_token", ""),
            **({"HX-Request": "true"} if htmx else {}),
        },
        data={"state": "dropped", "actor_is_admin": "false", "return_to": f"/instructor/cohorts/{cid}"},
        follow_redirects=False,
    )
    assert response.status_code == (200 if htmx else 303)
    assert response.headers["HX-Redirect" if htmx else "location"] == f"/instructor/cohorts/{cid}"
    a.db.expire_all()
    assert a.db.get(Enrollment, eid).status == "dropped"


@pytest.mark.parametrize("operation", ["state"])
def test_same_request_cookie_mutation_refuses_and_latches_even_after_restore(roster_actors, operation):
    from starlette.requests import Request

    actors, a = roster_actors, roster_actors.account
    cid, eid = _case(actors, target_role="instructor")
    request = Request(
        {"type": "http", "headers": list(actors.admin.request.scope["headers"]), "state": {"tenant": a.tenant}}
    )
    issued = boundary.change_web_roster_state(a.db, request=request, cohort_id=cid, person_id=a.pid, state="dropped")
    with pytest.raises(boundary.HumanOperationError):
        with issued as context:
            original = list(request.scope["headers"])
            request.scope["headers"][:] = actors.instructor.request.scope["headers"]
            with pytest.raises(boundary.HumanOperationError):
                context.revalidate()
            request.scope["headers"][:] = original
            with pytest.raises(boundary.HumanOperationError):
                context.revalidate()
    a.db.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert a.db.get(Enrollment, eid).status == "active"


@pytest.mark.parametrize("operation", ["state"])
def test_invalid_bound_constructor_and_owner_failure_clear_every_owned_request_alias(
    roster_actors, monkeypatch, operation
):
    actors, a = roster_actors, roster_actors.account
    cid, _ = _case(actors, target_role="instructor")

    def failure(*args, **kwargs):
        raise RuntimeError("injected required owner failure")

    monkeypatch.setattr(roster.learning, "set_roster_state", failure)
    with pytest.raises(boundary.HumanOperationError) as caught:
        _invoke(actors, operation, cid, admin=True)
    pending, seen, frames = [caught.value], set(), []
    while pending:
        error = pending.pop()
        if id(error) in seen:
            continue
        seen.add(id(error))
        traceback = error.__traceback__
        while traceback is not None:
            frame = traceback.tb_frame
            if frame.f_code.co_filename in {boundary.__file__, roster.__file__}:
                frames.append(frame.f_code.co_name)
                for alias in (
                    "request",
                    "self",
                    "human",
                    "context",
                    "resources",
                    "operation",
                    "raw",
                    "digest",
                    "candidate",
                    "claims",
                ):
                    assert frame.f_locals.get(alias) is None, "owned helper retained request or proof material"
            traceback = traceback.tb_next
        pending.extend(value for value in (error.__cause__, error.__context__) if value is not None)
    assert (
        "_human_operation" in frames
        and ("set_roster_state") in frames
    )
    with pytest.raises(boundary.HumanOperationError) as invalid:
        issued = boundary.change_web_roster_state(
            a.db, request=actors.admin.request, cohort_id="invalid", person_id=a.pid, state="dropped"
        )
        with issued:
            pytest.fail("invalid bound UUID reached owner handoff")
    traceback = invalid.value.__traceback__
    while traceback is not None:
        frame = traceback.tb_frame
        if frame.f_code.co_filename == boundary.__file__:
            assert frame.f_locals.get("request") is None and frame.f_locals.get("resources") is None
        traceback = traceback.tb_next
    a.db.commit()


@pytest.mark.parametrize("operation", ["state"])
def test_natural_owner_enrollment_wait_expiry_rolls_back_flushed_state_after_owner(
    roster_actors, monkeypatch, operation
):
    actors, a = roster_actors, roster_actors.account
    actor = actors.instructor
    cid, eid = _case(actors, target_role="student")
    a.admin.execute(
        update(AuthSession)
        .where(AuthSession.id == actor.session_id)
        .values(expires_at=datetime.now(UTC) + timedelta(seconds=2))
    )
    a.admin.commit()
    a.db.commit()
    sessions = sessionmaker(bind=a.db.get_bind(), autoflush=False)
    locked, started, release = Event(), Event(), Event()
    backend, calls = [], []
    original = roster.learning.set_roster_state

    def observe(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(roster.learning, "set_roster_state", observe)

    def holder():
        with sessions() as db:
            resolve_database_runtime().set_tenant(db, a.tid)
            db.scalar(select(Enrollment).where(Enrollment.id == eid).with_for_update())
            locked.set()
            assert release.wait(10)
            db.commit()

    def follower():
        with sessions() as db:
            resolve_database_runtime().set_tenant(db, a.tid)
            backend.append(db.scalar(text("SELECT pg_backend_pid()")))
            db.get(Cohort, cid).name = "Prior work survives owner wait"
            started.set()
            with pytest.raises(HTTPException) as caught:
                _invoke(actors, operation, cid, admin=False, db=db)
            assert caught.value.status_code == 303 and caught.value.headers == {"Location": "/login"}
            db.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(holder)
        assert locked.wait(10)
        second = pool.submit(follower)
        assert started.wait(10)
        try:
            resolve_database_runtime().set_tenant(a.db, a.tid)
            _wait_for_lock(a.db, backend[0])
            assert calls == [True]
            Event().wait(2.1)
        finally:
            release.set()
        first.result(timeout=20)
        second.result(timeout=20)
    a.db.expire_all()
    assert a.db.get(Enrollment, eid).status == "active"
    assert a.db.get(Cohort, cid).name == "Prior work survives owner wait"
