"""Hosted-only genuine WEB scheduling proof; actual Learning and canonical actors.

No installed/runtime qualification is claimed by these authored source tests.
Public owner wrappers below always execute the actual owner before injected faults.
"""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from copy import copy
from datetime import UTC, datetime, timedelta
from threading import Event
from uuid import UUID, uuid4

import dotmac_learning as learning
import pytest
from dotmac_kernel.audit import AuditEvent
from dotmac_kernel.models import AuthSession, PartyRoleGrant
from dotmac_kernel.session_runtime import resolve_database_runtime
from fastapi import HTTPException
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import sessionmaker

from app.models.class_session import ClassSession
from app.models.cohort import Cohort
from app.services import audit_context as boundary
from app.services import scheduling
from app.services.exceptions import BadRequestError
from legacy_probe.conftest import web_login
from legacy_probe.services.roster_request_fixtures import roster_actors
from legacy_probe.services.test_canonical_self_profile_locking import _wait_for_lock

assert roster_actors
NOW = datetime(2026, 10, 18, tzinfo=UTC)


def _case(actors):
    a = actors.account
    cohort = Cohort(tenant_id=a.tid, name="Prior cohort", discipline="networking", status="active")
    a.admin.add(cohort)
    a.admin.flush()
    session = scheduling.create_session(
        a.admin,
        tenant_id=a.tid,
        cohort_id=cohort.id,
        title="Before",
        starts_at=NOW,
        session_type="live_class",
        location="Room",
        notes="Note",
    )
    a.admin.commit()
    assert a.db.scalar(text("SELECT current_user")) == "app_user"
    return session.id, cohort.id


def _invoke(actors, sid, *, admin=False, db=None, **values):
    return scheduling.update_session(
        actors.account.db if db is None else db,
        request=(actors.admin if admin else actors.instructor).request,
        tenant_id=actors.account.tid,
        session_id=sid,
        **values,
    )


def _issued(actors, sid, **values):
    return boundary.update_web_session(
        actors.account.db,
        request=actors.instructor.request,
        session_id=sid,
        supplied=tuple(values.items()),
    )


def _events(db, sid):
    return db.scalars(
        select(AuditEvent).where(
            AuditEvent.action == "scheduling.update_session",
            AuditEvent.entity_id == str(sid),
        )
    ).all()


@pytest.mark.parametrize("admin", [False, True])
def test_real_actor_owner_fact_exact_audit_receipt_and_no_assigned_cohort_policy(roster_actors, admin):
    actors, a = roster_actors, roster_actors.account
    sid, _ = _case(actors)  # No instructor cohort assignment is created.
    root = a.db.get_transaction()
    fact, changes = _invoke(actors, sid, admin=admin, title=" After ")
    actor = actors.admin if admin else actors.instructor
    (event,) = _events(a.db, sid)
    assert type(fact) is learning.ClassSessionFact and fact.title == "After"
    assert changes == {"title": {"from": "Before", "to": "After"}}
    assert type(event.id) is UUID and event.tenant_id == a.tid
    assert (event.actor_type, event.actor_id, event.actor_party_id, event.actor_label) == (
        "user",
        str(actor.pid),
        actor.pid,
        actor.email,
    )
    assert event.source_application == "dotmac_academy_app"
    assert event.entity_type == "class_session" and event.entity_id == str(sid)
    assert event.details == {"changes": changes} and event.is_success is None
    assert event.occurred_at is not None and event.created_at is not None
    assert a.db.get_transaction() is root


def test_noop_is_private_completion_without_audit_and_none_differs_from_omission(roster_actors):
    actors, a = roster_actors, roster_actors.account
    sid, _ = _case(actors)
    with _issued(actors, sid) as context:
        result = context.update_session()
        assert result.changes == () and result.audit_event_id is None
        assert result.session.location == "Room" and result.session.notes == "Note"
    assert context.__dict__ == {} and _events(a.db, sid) == []
    fact, changes = _invoke(actors, sid, location=None)
    assert fact.location is None and fact.notes == "Note"
    assert changes == {"location": {"from": "Room", "to": None}}
    assert len(_events(a.db, sid)) == 1


@pytest.mark.parametrize(
    "failure", ["omitted", "repeat", "raw_audit", "copy", "supplied", "request", "root", "session", "tenant"]
)
def test_caught_private_command_refusals_poison_outer_commit_and_preserve_prior_work(roster_actors, failure):
    actors, a = roster_actors, roster_actors.account
    sid, cid = _case(actors)
    a.db.get(Cohort, cid).name = "Unrelated prior work"
    root = a.db.get_transaction()
    with pytest.raises(boundary.HumanOperationError):
        with _issued(actors, sid, title="After") as context:
            if failure == "omitted":
                pass
            elif failure == "repeat":
                context.update_session()
                with pytest.raises(boundary.HumanOperationError):
                    context.update_session()
            elif failure == "raw_audit":
                with pytest.raises(boundary.HumanOperationError):
                    context.audit(details={"changes": {}})
            elif failure == "copy":
                with pytest.raises(boundary.HumanOperationError):
                    copy(context).update_session()
            else:
                target, key = (
                    (context.resources, "supplied")
                    if failure == "supplied"
                    else (
                        context,
                        {"request": "request", "root": "root", "session": "session_id", "tenant": "tenant_id"}[failure],
                    )
                )
                old = getattr(target, key)
                value = (
                    (("title", "Substituted"),)
                    if failure == "supplied"
                    else (actors.admin.request if failure == "request" else uuid4())
                )
                object.__setattr__(target, key, value)
                with pytest.raises(boundary.HumanOperationError):
                    context.revalidate()
                object.__setattr__(target, key, old)
    assert context.__dict__ == {} and a.db.get_transaction() is root
    a.db.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert a.db.get(ClassSession, sid).title == "Before"
    assert a.db.get(Cohort, cid).name == "Unrelated prior work" and _events(a.db, sid) == []


@pytest.mark.parametrize(
    "mutation",
    [
        "result",
        "result_changes",
        "receipt",
        "fact",
        "audit_actor",
        "audit_details",
        "audit_time",
        "audit_success",
        "audit_delete",
    ],
)
def test_result_and_exact_persisted_audit_mutation_latch_even_when_caught(roster_actors, mutation):
    actors, a = roster_actors, roster_actors.account
    sid, _ = _case(actors)
    with pytest.raises(boundary.HumanOperationError):
        with _issued(actors, sid, title="After") as context:
            result = context.update_session()
            if mutation == "result":
                object.__setattr__(result.session, "title", "Substituted")
            elif mutation == "result_changes":
                object.__setattr__(result, "changes", ())
            elif mutation == "receipt":
                object.__setattr__(result, "audit_event_id", uuid4())
            elif mutation == "fact":
                a.db.execute(update(ClassSession).where(ClassSession.id == sid).values(title="Substituted"))
            elif mutation == "audit_delete":
                a.db.execute(delete(AuditEvent).where(AuditEvent.id == result.audit_event_id))
            else:
                key, value = {
                    "audit_actor": ("actor_id", str(actors.admin.pid)),
                    "audit_details": ("details", {"changes": {}}),
                    "audit_time": ("occurred_at", NOW),
                    "audit_success": ("is_success", True),
                }[mutation]
                a.db.execute(update(AuditEvent).where(AuditEvent.id == result.audit_event_id).values(**{key: value}))
            with pytest.raises((boundary.HumanOperationError, InvalidRequestError)):
                context.revalidate()
    a.db.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert a.db.get(ClassSession, sid).title == "Before" and _events(a.db, sid) == []


@pytest.mark.parametrize("error", ["owner", "database", "same_transaction_error", "role_loss"])
def test_failure_after_actual_public_owner_rolls_back_audit_and_fact(roster_actors, monkeypatch, error):
    actors, a = roster_actors, roster_actors.account
    sid, cid = _case(actors)
    a.db.get(Cohort, cid).name = "Prior work"
    root = a.db.get_transaction()
    actual = learning.update_session
    same = learning.TransactionChanged("actual owner transaction guard")

    def interrupted(db, command, **kwargs):
        result = actual(db, command, **kwargs)
        assert type(result.audit_event_id) is UUID
        if error == "owner":
            raise RuntimeError("required owner failure")
        if error == "database":
            db.execute(text("SELECT 1 / 0"))
        if error == "same_transaction_error":
            raise same
        if error == "role_loss":
            db.execute(delete(PartyRoleGrant).where(PartyRoleGrant.party_id == actors.instructor.pid))
        return result

    monkeypatch.setattr(learning, "update_session", interrupted)
    expected = (
        HTTPException
        if error == "role_loss"
        else (learning.TransactionChanged if error == "same_transaction_error" else boundary.HumanOperationError)
    )
    with pytest.raises(expected) as caught:
        _invoke(actors, sid, title="After")
    if error == "same_transaction_error":
        assert caught.value is same and caught.value.__suppress_context__ is True
    assert a.db.get_transaction() is root
    a.db.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert a.db.get(ClassSession, sid).title == "Before" and _events(a.db, sid) == []
    assert a.db.get(Cohort, cid).name == "Prior work"
    assert (
        a.db.scalar(
            select(func.count())
            .select_from(PartyRoleGrant)
            .where(
                PartyRoleGrant.party_id == actors.instructor.pid,
            )
        )
        == 1
    )


def test_actual_owner_update_refreshes_stale_orm_before_recording_from_value(roster_actors):
    actors, a = roster_actors, roster_actors.account
    sid, _ = _case(actors)
    stale = a.db.get(ClassSession, sid)
    a.admin.execute(update(ClassSession).where(ClassSession.id == sid).values(title="Concurrent"))
    a.admin.commit()
    assert stale.title == "Before"
    fact, changes = _invoke(actors, sid, title="After")
    assert fact.title == "After" and changes == {"title": {"from": "Concurrent", "to": "After"}}
    assert _events(a.db, sid)[0].details == {"changes": changes}


@pytest.mark.parametrize("failure", ["expiry", "role_loss_after_wait"])
def test_actual_owner_row_wait_revalidates_authority_and_rolls_back_completion(roster_actors, monkeypatch, failure):
    actors, a = roster_actors, roster_actors.account
    sid, cid = _case(actors)
    if failure == "expiry":
        a.admin.execute(
            update(AuthSession)
            .where(AuthSession.id == actors.instructor.session_id)
            .values(
                expires_at=datetime.now(UTC) + timedelta(seconds=2),
            )
        )
        a.admin.commit()
    a.db.commit()
    sessions = sessionmaker(bind=a.db.get_bind(), autoflush=False)
    locked, started, release = Event(), Event(), Event()
    backend = []
    actual = learning.update_session

    def after_owner(db, command, **kwargs):
        result = actual(db, command, **kwargs)  # This genuine owner is the blocked row-lock acquirer.
        if failure == "role_loss_after_wait":
            db.execute(delete(PartyRoleGrant).where(PartyRoleGrant.party_id == actors.instructor.pid))
        return result

    monkeypatch.setattr(learning, "update_session", after_owner)

    def holder():
        with sessions() as db:
            resolve_database_runtime().set_tenant(db, a.tid)
            db.scalar(select(ClassSession).where(ClassSession.id == sid).with_for_update())
            locked.set()
            assert release.wait(10)
            db.commit()

    def follower():
        with sessions() as db:
            resolve_database_runtime().set_tenant(db, a.tid)
            backend.append(db.scalar(text("SELECT pg_backend_pid()")))
            db.get(Cohort, cid).name = "Prior wait work"
            started.set()
            with pytest.raises(HTTPException) as caught:
                _invoke(actors, sid, db=db, title="After")
            assert caught.value.status_code == (303 if failure == "expiry" else 403)
            db.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(holder)
        assert locked.wait(10)
        second = pool.submit(follower)
        assert started.wait(10)
        try:
            resolve_database_runtime().set_tenant(a.db, a.tid)
            _wait_for_lock(a.db, backend[0])
            if failure == "expiry":
                Event().wait(2.1)
        finally:
            release.set()
        first.result(timeout=20)
        second.result(timeout=20)
    a.db.expire_all()
    assert a.db.get(ClassSession, sid).title == "Before" and _events(a.db, sid) == []
    assert a.db.get(Cohort, cid).name == "Prior wait work"


def test_invalid_update_caught_outer_commit_does_not_keep_partial_title(roster_actors):
    actors, a = roster_actors, roster_actors.account
    sid, _ = _case(actors)
    with pytest.raises(BadRequestError, match="ends_at must be after starts_at"):
        _invoke(actors, sid, title="After", ends_at=NOW - timedelta(hours=1))
    a.db.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert a.db.get(ClassSession, sid).title == "Before" and _events(a.db, sid) == []


@pytest.mark.parametrize("argument", ["callback", "result", "audit_event_id", "no_audit"])
def test_supplied_execution_or_completion_is_refused_and_poisoned(roster_actors, argument):
    actors, a = roster_actors, roster_actors.account
    sid, _ = _case(actors)
    with pytest.raises(boundary.HumanOperationError):
        with _issued(actors, sid, title="After") as context:
            with pytest.raises(boundary.HumanOperationError):
                context.update_session(**{argument: None})
            with pytest.raises(boundary.HumanOperationError):
                context.update_session()
    a.db.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert a.db.get(ClassSession, sid).title == "Before" and _events(a.db, sid) == []


@pytest.mark.parametrize("htmx", [False, True])
def test_real_login_edit_route_keeps_parsing_and_redirects(roster_actors, app_client, htmx):
    actors, a = roster_actors, roster_actors.account
    sid, cid = _case(actors)
    host = {"Host": f"{a.tenant.slug}.localhost"}
    web_login(app_client, host, actors.instructor.email, "chosen-password9")
    app_client.get("/instructor/cohorts", headers=host)
    headers = {**host, "x-csrf-token": app_client.cookies.get("csrf_token", "")}
    if htmx:
        headers["HX-Request"] = "true"
    missing = app_client.post(
        f"/instructor/sessions/{sid}/edit",
        headers=headers,
        data={"title": "After", "starts_at": "", "session_type": "live_class"},
        follow_redirects=False,
    )
    assert missing.status_code == 422
    body = missing.json()
    assert body["code"] == "validation_error"
    assert any(
        error["loc"] == ["body", "starts_at"] and error["msg"] == "Field required" for error in body["details"]
    )
    response = app_client.post(
        f"/instructor/sessions/{sid}/edit",
        headers=headers,
        data={"title": "After", "starts_at": "2026-10-18T14:00", "ends_at": "", "session_type": "live_class"},
        follow_redirects=False,
    )
    assert response.status_code == (200 if htmx else 303)
    assert response.headers["HX-Redirect" if htmx else "location"] == f"/instructor/cohorts/{cid}/timetable"
    a.db.expire_all()
    row = a.db.get(ClassSession, sid)
    assert row.title == "After" and row.starts_at.astimezone(UTC).hour == 13 and row.ends_at is None
    (event,) = _events(a.db, sid)
    assert event.actor_party_id == actors.instructor.pid and event.is_success is None


def test_owned_failure_frames_clear_request_context_and_command_aliases(roster_actors, monkeypatch):
    actors, a = roster_actors, roster_actors.account
    sid, _ = _case(actors)
    actual = learning.update_session

    def fail_after_owner(*args, **kwargs):
        actual(*args, **kwargs)
        raise RuntimeError("required owner failure")

    monkeypatch.setattr(learning, "update_session", fail_after_owner)
    with pytest.raises(boundary.HumanOperationError) as caught:
        _invoke(actors, sid, title="After")
    pending, seen, owned = [caught.value], set(), []
    while pending:
        error = pending.pop()
        if id(error) in seen:
            continue
        seen.add(id(error))
        traceback = error.__traceback__
        while traceback is not None:
            frame = traceback.tb_frame
            if frame.f_code.co_filename in {boundary.__file__, scheduling.__file__}:
                owned.append((frame.f_code.co_filename, frame.f_code.co_name))
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
                    "command",
                    "attribution",
                    "args",
                    "kwargs",
                ):
                    assert frame.f_locals.get(alias) is None
            traceback = traceback.tb_next
        pending.extend(value for value in (error.__cause__, error.__context__) if value is not None)
    # Two different functions are named update_session in the owned files, so count per file and name.
    assert Counter(owned) == {
        (scheduling.__file__, "update_session"): 2,
        (boundary.__file__, "_human_operation"): 2,
        (boundary.__file__, "update_session"): 1,
    }
    a.db.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert a.db.get(ClassSession, sid).title == "Before" and _events(a.db, sid) == []


def test_direct_edit_route_missing_start_keeps_400_with_genuine_request_and_account(roster_actors):
    from app.services import web_auth
    from app.web import instructor

    actors, a = roster_actors, roster_actors.account
    sid, _ = _case(actors)
    request = actors.instructor.request
    person = web_auth.require_web_user(request, db=a.db)
    person = web_auth.require_web_role("instructor")(request, person=person, db=a.db)
    assert person.id == actors.instructor.pid and person.tenant_id == a.tid
    root = a.db.get_transaction()
    with pytest.raises(HTTPException) as caught:
        instructor.edit_class_session(
            sid,
            request=request,
            title="After",
            starts_at="",
            session_type="live_class",
            ends_at="",
            location="",
            join_url="",
            notes="",
            person=person,
            db=a.db,
        )
    assert caught.value.status_code == 400 and caught.value.detail == "A start time is required."
    assert a.db.get_transaction() is root
    assert a.db.get(ClassSession, sid).title == "Before" and _events(a.db, sid) == []
