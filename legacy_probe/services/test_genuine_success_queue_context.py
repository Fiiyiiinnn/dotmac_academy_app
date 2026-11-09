"""Hosted-only actual Success Queue owner and genuine WEB authority canaries.

Authored source tests: no installed owner, registry, runtime or hosted claim.
Fault wrappers execute actual owners/delivery before injecting failures.
"""

from concurrent.futures import ThreadPoolExecutor
from copy import copy
from datetime import UTC, datetime, timedelta
from threading import Event
from uuid import UUID, uuid4

import dotmac_learning as learning
import pytest
from dotmac_kernel.audit import AuditEvent
from dotmac_kernel.models import AuthSession, Party, PartyRoleGrant
from dotmac_kernel.session_runtime import resolve_database_runtime
from fastapi import HTTPException
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import sessionmaker

from app.models.cohort import Cohort
from app.models.email_outbox import EmailOutbox
from app.models.identity_extensions import AcademyAccountProfile
from app.models.notification import Notification
from app.models.success_queue import REASON_INACTIVITY, SuccessQueueEntry
from app.services import audit_context as boundary
from app.services import success_queue
from app.services.exceptions import BadRequestError, NotFoundError
from legacy_probe.services.roster_request_fixtures import roster_actors
from legacy_probe.services.test_canonical_self_profile_locking import _wait_for_lock
from legacy_probe.services.test_success_queue import _entries, _seed

assert roster_actors


def _case(actors):
    a = actors.account
    person, cohort, *_ = _seed(a.admin, a.tenant, email=f"success-{uuid4().hex}@a.edu")
    success_queue.sweep(a.admin, tenant_id=a.tid)
    a.admin.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    (entry,) = _entries(a.db, a.tenant, person, REASON_INACTIVITY)
    assert a.db.scalar(text("SELECT current_user")) == "app_user"
    return person.id, cohort.id, entry.id


def _invoke(actors, eid, *, action="assign", db=None, admin=False):
    return success_queue.transition(
        actors.account.db if db is None else db,
        request=(actors.admin if admin else actors.instructor).request,
        tenant_id=actors.account.tid,
        entry_id=eid,
        action=action,
    )


def _message(actors, cid, **values):
    return success_queue.message_segment(
        actors.account.db,
        request=actors.instructor.request,
        tenant_id=actors.account.tid,
        cohort_id=cid,
        segment=values.pop("segment", "inactive"),
        subject=values.pop("subject", " Check <in> "),
        body=values.pop("body", "  Hello <learner> & friend\nNext line  "),
        **values,
    )


def _issued(actors, eid):
    return boundary.transition_web_success_entry(
        actors.account.db, request=actors.instructor.request, entry_id=eid, action="assign"
    )


def _events(db, entity_id):
    return db.scalars(
        select(AuditEvent).where(AuditEvent.entity_id == str(entity_id), AuditEvent.action.like("success_queue.%"))
    ).all()


def _count(db, model):
    return db.scalar(select(func.count()).select_from(model))


@pytest.mark.parametrize("admin", [False, True])
def test_real_staff_owner_queue_fact_and_mandatory_same_assignment_receipts(roster_actors, admin):
    actors, a = roster_actors, roster_actors.account
    pid, _, eid = _case(actors)
    actor = actors.admin if admin else actors.instructor
    root = a.db.get_transaction()
    first = _invoke(actors, eid, admin=admin)
    second = _invoke(actors, eid, admin=admin)
    assert type(first) is learning.QueueFact and type(second) is learning.QueueFact
    assert first.assigned_to == second.assigned_to == actor.pid
    assert first.status == second.status == "acknowledged"
    events = _events(a.db, eid)
    assert len(events) == 2 and events[0].id != events[1].id
    for event in events:
        assert type(event.id) is UUID
        assert (event.tenant_id, event.actor_type, event.actor_id, event.actor_party_id, event.actor_label) == (
            a.tid,
            "user",
            str(actor.pid),
            actor.pid,
            actor.email,
        )
        assert (event.source_application, event.action, event.entity_type, event.entity_id) == (
            "dotmac_academy_app",
            "success_queue.assign",
            "success_queue_entry",
            str(eid),
        )
        assert event.details == {"reason": REASON_INACTIVITY, "person_id": str(pid)}
        assert event.is_success is None and event.occurred_at is not None and event.created_at is not None
    assert a.db.get_transaction() is root


@pytest.mark.parametrize(
    "failure",
    ["omission", "repeat", "audit", "copy", "receipt", "callback", "no_audit", "request", "root", "session", "tenant"],
)
def test_private_refusal_is_sticky_and_caught_outer_commit_preserves_prior_work(roster_actors, failure):
    actors, a = roster_actors, roster_actors.account
    _, cid, eid = _case(actors)
    a.db.get(Cohort, cid).name = "Unrelated prior work"
    root = a.db.get_transaction()
    with pytest.raises(boundary.HumanOperationError):
        with _issued(actors, eid) as context:
            if failure == "omission":
                pass
            elif failure == "repeat":
                context.transition_success_entry()
                with pytest.raises(boundary.HumanOperationError):
                    context.transition_success_entry()
            elif failure == "audit":
                with pytest.raises(boundary.HumanOperationError):
                    context.audit()
            elif failure == "copy":
                with pytest.raises(boundary.HumanOperationError):
                    copy(context).transition_success_entry()
            elif failure in {"receipt", "callback", "no_audit"}:
                with pytest.raises(boundary.HumanOperationError):
                    context.transition_success_entry(**{failure: None})
            else:
                key = {"request": "request", "root": "root", "session": "session_id", "tenant": "tenant_id"}[failure]
                old = getattr(context, key)
                setattr(context, key, actors.admin.request if failure == "request" else uuid4())
                with pytest.raises(boundary.HumanOperationError):
                    context.revalidate()
                setattr(context, key, old)
    assert context.__dict__ == {} and a.db.get_transaction() is root
    a.db.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert a.db.get(SuccessQueueEntry, eid).status == "open" and _events(a.db, eid) == []
    assert a.db.get(Cohort, cid).name == "Unrelated prior work"


@pytest.mark.parametrize("mutation", ["fact", "result", "receipt", "actor", "details", "time", "success", "delete"])
def test_exact_queue_and_actual_audit_tampering_roll_back(roster_actors, mutation):
    actors, a = roster_actors, roster_actors.account
    _, _, eid = _case(actors)
    with pytest.raises(boundary.HumanOperationError):
        with _issued(actors, eid) as context:
            effect = context.transition_success_entry()
            if mutation == "fact":
                a.db.execute(update(SuccessQueueEntry).where(SuccessQueueEntry.id == eid).values(status="resolved"))
            elif mutation == "result":
                object.__setattr__(effect.entry, "assigned_to", actors.admin.pid)
            elif mutation == "receipt":
                object.__setattr__(effect, "audit_event_id", uuid4())
            elif mutation == "delete":
                a.db.execute(delete(AuditEvent).where(AuditEvent.id == effect.audit_event_id))
            else:
                key, value = {
                    "actor": ("actor_party_id", actors.admin.pid),
                    "details": ("details", {}),
                    "time": ("occurred_at", datetime(2000, 1, 1, tzinfo=UTC)),
                    "success": ("is_success", True),
                }[mutation]
                a.db.execute(update(AuditEvent).where(AuditEvent.id == effect.audit_event_id).values(**{key: value}))
            with pytest.raises((boundary.HumanOperationError, InvalidRequestError)):
                context.revalidate()
    a.db.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert a.db.get(SuccessQueueEntry, eid).status == "open" and _events(a.db, eid) == []


@pytest.mark.parametrize("mutation", ["revoked", "expired", "suspended", "inactive", "role"])
@pytest.mark.parametrize("message", [False, True])
def test_current_authority_refuses_actual_owner_writes(roster_actors, mutation, message):
    actors, a = roster_actors, roster_actors.account
    _, cid, eid = _case(actors)
    actor = actors.instructor
    if mutation in {"revoked", "expired"}:
        values = (
            {"revoked_at": datetime.now(UTC)}
            if mutation == "revoked"
            else {"expires_at": datetime.now(UTC) - timedelta(seconds=1)}
        )
        a.admin.execute(update(AuthSession).where(AuthSession.id == actor.session_id).values(**values))
    elif mutation == "suspended":
        a.admin.execute(
            update(AcademyAccountProfile).where(AcademyAccountProfile.party_id == actor.pid).values(status="suspended")
        )
    elif mutation == "inactive":
        a.admin.execute(update(Party).where(Party.id == actor.pid).values(is_active=False))
    else:
        a.admin.execute(delete(PartyRoleGrant).where(PartyRoleGrant.party_id == actor.pid))
    a.admin.commit()
    with pytest.raises(HTTPException) as caught:
        _message(actors, cid) if message else _invoke(actors, eid)
    assert caught.value.status_code == (403 if mutation == "role" else 303)
    a.db.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert _events(a.db, eid) == _events(a.db, cid) == []
    assert _count(a.db, Notification) == _count(a.db, EmailOutbox) == 0


def test_wrong_tenant_missing_entry_cohort_invalid_segment_and_empty_trim(roster_actors, tenant_b):
    actors, a = roster_actors, roster_actors.account
    _, cid, eid = _case(actors)
    with pytest.raises(boundary.HumanOperationError):
        success_queue.transition(
            a.db, request=actors.instructor.request, tenant_id=tenant_b.id, entry_id=eid, action="assign"
        )
    with pytest.raises(NotFoundError):
        _invoke(actors, uuid4())
    foreign = Cohort(tenant_id=tenant_b.id, name="Other tenant", discipline="fiber", status="active")
    a.admin.add(foreign)
    a.admin.commit()
    with pytest.raises(NotFoundError):
        _message(actors, foreign.id)
    with pytest.raises(BadRequestError):
        _message(actors, cid, segment="unknown")
    with pytest.raises(BadRequestError):
        _message(actors, cid, subject=" \n ")
    assert _count(a.db, Notification) == _count(a.db, EmailOutbox) == 0


@pytest.mark.parametrize("population", ["normal", "zero", "missing_email", "inactive_party", "suspended_profile"])
def test_exact_delivery_render_and_zero_or_excluded_recipient_audit(roster_actors, monkeypatch, population):
    actors, a = roster_actors, roster_actors.account
    pid, cid, _ = _case(actors)
    if population == "missing_email":
        a.admin.execute(update(Party).where(Party.id == pid).values(email=None))
    elif population == "inactive_party":
        a.admin.execute(update(Party).where(Party.id == pid).values(is_active=False))
    elif population == "suspended_profile":
        a.admin.execute(
            update(AcademyAccountProfile).where(AcademyAccountProfile.party_id == pid).values(status="suspended")
        )
    a.admin.commit()
    actual_terms, snapshots = boundary.success_terms, []

    def terms(db):
        result = actual_terms(db)
        snapshots.append(result)
        return result

    monkeypatch.setattr(boundary, "success_terms", terms)
    result = _message(
        actors,
        cid,
        segment="failed_final" if population == "zero" else "inactive",
        now=datetime.now(UTC) + timedelta(days=1),
    )
    expected = int(population in {"normal", "missing_email"})
    assert len(snapshots) == 1 and result["recipients"] == expected
    (event,) = _events(a.db, cid)
    assert event.details == {
        "segment": "failed_final" if population == "zero" else "inactive",
        "recipients": expected,
        "subject": "Check <in>",
        "action_id": result["action_id"],
    }
    assert event.actor_party_id == actors.instructor.pid and event.is_success is None
    assert _count(a.db, Notification) == expected
    outbox = a.db.scalars(select(EmailOutbox)).all()
    assert len(outbox) == int(population == "normal")
    if expected:
        notification = a.db.scalars(select(Notification)).one()
        assert (
            notification.person_id,
            notification.kind,
            notification.title,
            notification.body,
            notification.link,
        ) == (
            pid,
            "instructor_message",
            "Check <in>",
            "Hello <learner> & friend\nNext line",
            "/",
        )
    if outbox:
        assert outbox[0].idempotency_key == f"segmsg:{result['action_id']}:{pid}"
        assert outbox[0].html_body == "<p>Hello &lt;learner&gt; &amp; friend<br>Next line</p>"
        assert outbox[0].text_body == "Hello <learner> & friend\nNext line"
        assert outbox[0].payload == {"segment": "inactive", "cohort_id": str(cid)}


@pytest.mark.parametrize("stage", ["notification", "outbox", "audit", "owner", "transaction", "late_role"])
def test_actual_delivery_and_owner_failure_caught_commit_has_no_operation_effects(roster_actors, monkeypatch, stage):
    actors, a = roster_actors, roster_actors.account
    _, cid, _ = _case(actors)
    a.db.get(Cohort, cid).name = "Prior work"
    root = a.db.get_transaction()
    same = learning.TransactionChanged("owner transaction changed")
    if stage in {"notification", "outbox"}:
        target, key = (boundary.notifications, "notify") if stage == "notification" else (boundary, "enqueue_email")
        actual = getattr(target, key)

        def interrupted(*args, **kwargs):
            actual(*args, **kwargs)
            raise RuntimeError("required delivery failed")
    elif stage == "audit":
        # Execute the actual owner audit writer before injecting the required failure.
        import dotmac_learning.success_queue as owner

        target, key = owner, "write_audit_event"
        actual = getattr(target, key)

        def interrupted(*args, **kwargs):
            actual(*args, **kwargs)
            raise RuntimeError("required audit failed")
    else:
        target, key = learning, "message_segment_with_receipt"
        actual = getattr(target, key)

        def interrupted(db, **kwargs):
            result = actual(db, **kwargs)
            if stage == "transaction":
                raise same
            if stage == "late_role":
                db.execute(delete(PartyRoleGrant).where(PartyRoleGrant.party_id == actors.instructor.pid))
                return result
            raise RuntimeError("required owner failed")

    monkeypatch.setattr(target, key, interrupted)
    expected = (
        HTTPException
        if stage == "late_role"
        else (learning.TransactionChanged if stage == "transaction" else boundary.HumanOperationError)
    )
    with pytest.raises(expected) as caught:
        _message(actors, cid)
    if stage == "transaction":
        assert caught.value is same
    assert a.db.get_transaction() is root
    a.db.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert _events(a.db, cid) == [] and _count(a.db, Notification) == _count(a.db, EmailOutbox) == 0
    assert a.db.get(Cohort, cid).name == "Prior work"


@pytest.mark.parametrize("mutation", ["receipt", "result", "audit", "notification", "outbox", "observations"])
def test_message_actual_receipt_result_and_consequence_tamper_poisoned(roster_actors, mutation):
    actors, a = roster_actors, roster_actors.account
    _, cid, _ = _case(actors)
    with pytest.raises(boundary.HumanOperationError):
        with boundary.message_web_success_segment(
            a.db, request=actors.instructor.request, cohort_id=cid, segment="inactive", subject="Subject", body="Body"
        ) as context:
            effect = context.message_success_segment()
            if mutation == "receipt":
                object.__setattr__(effect, "audit_event_id", uuid4())
            elif mutation == "result":
                object.__setattr__(effect.result, "recipients", 0)
            elif mutation == "audit":
                a.db.execute(update(AuditEvent).where(AuditEvent.id == effect.audit_event_id).values(details={}))
            elif mutation == "notification":
                a.db.execute(update(Notification).values(body="Substituted"))
            elif mutation == "outbox":
                a.db.execute(update(EmailOutbox).values(html_body="Substituted"))
            else:
                context._success_deliveries = ()
            with pytest.raises(boundary.HumanOperationError):
                context.revalidate()
    a.db.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert _events(a.db, cid) == [] and _count(a.db, Notification) == _count(a.db, EmailOutbox) == 0


@pytest.mark.parametrize("failure", ["expiry", "role_loss_after_wait"])
def test_actual_owner_queue_lock_wait_rechecks_current_authority(roster_actors, monkeypatch, failure):
    actors, a = roster_actors, roster_actors.account
    _, cid, eid = _case(actors)
    if failure == "expiry":
        a.admin.execute(
            update(AuthSession)
            .where(AuthSession.id == actors.instructor.session_id)
            .values(expires_at=datetime.now(UTC) + timedelta(seconds=2))
        )
        a.admin.commit()
    a.db.commit()
    sessions = sessionmaker(bind=a.db.get_bind(), autoflush=False)
    locked, started, release = Event(), Event(), Event()
    backend = []
    actual = learning.transition_success_entry_with_receipt

    def after_owner(db, **kwargs):
        result = actual(db, **kwargs)
        if failure == "role_loss_after_wait":
            db.execute(delete(PartyRoleGrant).where(PartyRoleGrant.party_id == actors.instructor.pid))
        return result

    monkeypatch.setattr(learning, "transition_success_entry_with_receipt", after_owner)

    def holder():
        with sessions() as db:
            resolve_database_runtime().set_tenant(db, a.tid)
            db.scalar(select(SuccessQueueEntry).where(SuccessQueueEntry.id == eid).with_for_update())
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
                _invoke(actors, eid, db=db)
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
    assert a.db.get(SuccessQueueEntry, eid).status == "open" and _events(a.db, eid) == []
    assert a.db.get(Cohort, cid).name == "Prior wait work"


def test_prior_real_audit_receipt_cannot_complete_new_same_assignment(roster_actors, monkeypatch):
    actors, a = roster_actors, roster_actors.account
    _, _, eid = _case(actors)
    with _issued(actors, eid) as context:
        prior = context.transition_success_entry()
    actual = learning.transition_success_entry_with_receipt

    def stale_receipt(db, **kwargs):
        current = actual(db, **kwargs)
        assert current.audit_event_id != prior.audit_event_id
        return prior

    monkeypatch.setattr(learning, "transition_success_entry_with_receipt", stale_receipt)
    with pytest.raises(boundary.HumanOperationError):
        _invoke(actors, eid)
    a.db.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert [event.id for event in _events(a.db, eid)] == [prior.audit_event_id]
    assert a.db.get(SuccessQueueEntry, eid).assigned_to == actors.instructor.pid


def test_changed_actor_email_longer_than_label_bound_omits_optional_snapshot(roster_actors):
    actors, a = roster_actors, roster_actors.account
    _, _, eid = _case(actors)
    a.admin.execute(update(Party).where(Party.id == actors.instructor.pid).values(email="a" * 159 + "@a.edu"))
    a.admin.commit()
    _invoke(actors, eid)
    (event,) = _events(a.db, eid)
    assert event.actor_label is None and event.actor_party_id == actors.instructor.pid


def test_notify_then_email_uses_exact_owner_action_and_delivery_sequence(roster_actors, monkeypatch):
    actors, a = roster_actors, roster_actors.account
    pid, cid, _ = _case(actors)
    observed = []
    actual_notify, actual_enqueue = boundary.notifications.notify, boundary.enqueue_email

    def notify(db, **kwargs):
        row = actual_notify(db, **kwargs)
        observed.append(("notify", kwargs["person_id"], db))
        return row

    def enqueue(db, **kwargs):
        result = actual_enqueue(db, **kwargs)
        observed.append(("email", kwargs["idempotency_key"], db))
        return result

    monkeypatch.setattr(boundary.notifications, "notify", notify)
    monkeypatch.setattr(boundary, "enqueue_email", enqueue)
    result = _message(actors, cid)
    assert observed == [("notify", pid, a.db), ("email", f"segmsg:{result['action_id']}:{pid}", a.db)]


def test_transition_actual_owner_failure_preserves_transaction_changed_instance(roster_actors, monkeypatch):
    actors, a = roster_actors, roster_actors.account
    _, _, eid = _case(actors)
    actual = learning.transition_success_entry_with_receipt
    same = learning.TransactionChanged("actual owner transaction guard")

    def interrupted(db, **kwargs):
        result = actual(db, **kwargs)
        assert type(result.audit_event_id) is UUID
        raise same

    monkeypatch.setattr(learning, "transition_success_entry_with_receipt", interrupted)
    with pytest.raises(learning.TransactionChanged) as caught:
        _invoke(actors, eid)
    assert caught.value is same
    a.db.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert a.db.get(SuccessQueueEntry, eid).status == "open" and _events(a.db, eid) == []


@pytest.mark.parametrize("stage", ["audit", "owner", "database", "late_role"])
def test_transition_actual_failure_points_roll_back_queue_audit_and_keep_prior_work(roster_actors, monkeypatch, stage):
    actors, a = roster_actors, roster_actors.account
    _, cid, eid = _case(actors)
    a.db.get(Cohort, cid).name = "Prior transition work"
    root = a.db.get_transaction()
    if stage == "audit":
        import dotmac_learning.success_queue as owner

        target, key = owner, "write_audit_event"
        actual = getattr(target, key)

        def interrupted(*args, **kwargs):
            actual(*args, **kwargs)
            raise RuntimeError("required transition audit failed")
    else:
        target, key = learning, "transition_success_entry_with_receipt"
        actual = getattr(target, key)

        def interrupted(db, **kwargs):
            result = actual(db, **kwargs)
            if stage == "database":
                db.execute(text("SELECT 1 / 0"))
            elif stage == "late_role":
                db.execute(delete(PartyRoleGrant).where(PartyRoleGrant.party_id == actors.instructor.pid))
            else:
                raise RuntimeError("required transition owner failed")
            return result

    monkeypatch.setattr(target, key, interrupted)
    with pytest.raises(HTTPException if stage == "late_role" else boundary.HumanOperationError):
        _invoke(actors, eid)
    assert a.db.get_transaction() is root
    a.db.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert a.db.get(SuccessQueueEntry, eid).status == "open" and _events(a.db, eid) == []
    assert a.db.get(Cohort, cid).name == "Prior transition work"


def test_transition_actual_owner_reads_current_row_before_status_validation(roster_actors):
    actors, a = roster_actors, roster_actors.account
    _, _, eid = _case(actors)
    stale = a.db.get(SuccessQueueEntry, eid)
    a.admin.execute(update(SuccessQueueEntry).where(SuccessQueueEntry.id == eid).values(status="resolved"))
    a.admin.commit()
    assert stale.status == "open"
    with pytest.raises(BadRequestError):
        _invoke(actors, eid, action="acknowledge")
    a.db.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert a.db.get(SuccessQueueEntry, eid).status == "resolved" and _events(a.db, eid) == []
