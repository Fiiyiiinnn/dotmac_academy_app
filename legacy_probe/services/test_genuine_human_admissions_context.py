"""Hosted genuine human Admissions canaries, authored SOURCE ONLY / NOT RUN.

Actual canonical accounts, HMAC/JWT sessions, private command execution, real
Learning enrollment and actual canonical audit/outbox rows. Fault wrappers below
run actual producers before failing; no positive owner/registry substitute.
Assessment-history/Integration receiver qualification is explicitly separate.
"""

from concurrent.futures import ThreadPoolExecutor
from copy import copy
from datetime import UTC, datetime, timedelta
from threading import Event
from types import SimpleNamespace
from uuid import UUID, uuid4

import dotmac_learning as learning
import pytest
from dotmac_kernel.audit import AuditEvent
from dotmac_kernel.models import AuthSession, Party, PartyRoleGrant, Role, UserCredential
from dotmac_kernel.session_runtime import resolve_database_runtime
from fastapi import HTTPException
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from app.models.admissions import Applicant
from app.models.cohort import Cohort, Enrollment
from app.models.email_outbox import EmailOutbox
from app.models.identity_extensions import AcademyAccountProfile
from app.models.onboarding import OnboardingTask
from app.models.track import CohortTrack, Track
from app.services import admissions, applicant_email, entrance_exam, onboarding
from app.services import audit_context as boundary
from app.services.exceptions import BadRequestError, ConflictError, NotFoundError
from app.services.security import hash_password
from legacy_probe.services.roster_request_fixtures import roster_actors
from legacy_probe.services.test_canonical_invitation_issuance import _credential, _person
from legacy_probe.services.test_canonical_self_profile_locking import _wait_for_lock
from legacy_probe.services.test_entrance_exam import _bank_with_questions

assert roster_actors


def _request(actors, *, transport="web", tenant=None):
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
        web=_request(actors),
        api=_request(actors, transport="api"),
    )


def _review(c, action="accept", **kwargs):
    return admissions.review_web_applicant(
        c.a.db, request=c.web, applicant_id=c.id, action=action, reason="Reason", **kwargs
    )


def _transition(c, status, *, db=None, request=None):
    return admissions.transition_api_applicant(
        c.a.db if db is None else db,
        request=c.api if request is None else request,
        applicant_id=c.id,
        to_status=status,
        notes="Reason",
    )


def _events(c):
    return c.a.db.scalars(
        select(AuditEvent).where(
            AuditEvent.tenant_id == c.a.tid, AuditEvent.entity_type == "applicant", AuditEvent.entity_id == str(c.id)
        )
    ).all()


def _outbox_scope(c):
    # Onboarding's unchanged producer has an empty payload; its canonical key
    # identifies the applicant and the particular minted token intent.
    return (
        EmailOutbox.tenant_id == c.a.tid,
        (EmailOutbox.payload["applicant_id"].astext == str(c.id))
        | (
            (EmailOutbox.kind == "onboarding_invite")
            & EmailOutbox.idempotency_key.startswith(f"onboarding-invite:{c.id}:")
        ),
    )


def _snapshot(c):
    db = c.a.db
    db.expire_all()
    return (
        tuple(boundary._persisted_values(row) for row in db.scalars(select(Applicant).where(Applicant.id == c.id))),
        tuple(
            boundary._persisted_values(row)
            for row in db.scalars(
                select(OnboardingTask).where(OnboardingTask.applicant_id == c.id).order_by(OnboardingTask.id)
            )
        ),
        tuple(
            boundary._persisted_values(row)
            for row in db.scalars(select(EmailOutbox).where(*_outbox_scope(c)).order_by(EmailOutbox.id))
        ),
        tuple(
            boundary._persisted_values(row)
            for row in db.scalars(select(AuditEvent).where(AuditEvent.entity_id == str(c.id)).order_by(AuditEvent.id))
        ),
    )


def test_actual_web_accept_has_three_ordered_exact_canonical_receipts_and_tasks(admission_case):
    c = admission_case
    root = c.a.db.get_transaction()
    with boundary.review_web_applicant(
        c.a.db, request=c.web, applicant_id=c.id, action="accept", reason="Reason"
    ) as context:
        result = context.execute_admission()
        trace = tuple(
            (row.id, row.details, row.is_success, row.actor_party_id) for row, _, _ in context._admission.receipts
        )
        assert result.status == "onboarding" and result.onboarding_token_hash
        assert [details for _, details, _, _ in trace] == [
            {"from_status": "applied", "to_status": "screened", "reason": "Reason", "source": "admin_web"},
            {"from_status": "screened", "to_status": "accepted", "reason": "Reason", "source": "admin_web"},
            {"from_status": "accepted", "to_status": "onboarding", "reason": "Reason", "source": "admin_web"},
        ]
    assert context.__dict__ == {} and c.a.db.get_transaction() is root
    events = _events(c)
    assert {event.id for event in events} == {key for key, *_ in trace} and len(events) == 3
    for event in events:
        assert type(event.id) is UUID and event.is_success is None
        assert (event.actor_type, event.actor_id, event.actor_party_id, event.actor_label) == (
            "user",
            str(c.actors.admin.pid),
            c.actors.admin.pid,
            c.actors.admin.email,
        )
        assert event.source_application == "dotmac_academy_app" and event.occurred_at and event.created_at
    assert len(onboarding.list_tasks(c.a.db, tenant_id=c.a.tid, applicant_id=c.id)) == len(
        onboarding.onboarding_policy(c.a.db).task_catalog
    )
    row = c.a.db.scalars(select(EmailOutbox).where(*_outbox_scope(c))).one()
    assert c.email and row.recipient == c.email and row.kind == "onboarding_invite"
    assert row.payload == {}
    assert row.idempotency_key == f"onboarding-invite:{c.id}:{result.onboarding_token_hash}"


def test_api_same_status_zero_trace_and_same_onboarding_still_remints_and_enqueues(admission_case):
    c = admission_case
    _transition(c, "applied")
    assert _events(c) == []
    _transition(c, "screened")
    _transition(c, "accepted")
    _transition(c, "onboarding")
    prior = c.a.db.get(Applicant, c.id).onboarding_token_hash
    count = len(_events(c))
    assert count == 3 and c.email
    _transition(c, "onboarding")
    assert c.a.db.get(Applicant, c.id).onboarding_token_hash != prior and len(_events(c)) == count
    rows = c.a.db.scalars(select(EmailOutbox).where(*_outbox_scope(c), EmailOutbox.kind == "onboarding_invite")).all()
    assert len(rows) == 2 and len({row.idempotency_key for row in rows}) == 2
    assert {row.idempotency_key for row in rows} == {
        f"onboarding-invite:{c.id}:{prior}",
        f"onboarding-invite:{c.id}:{c.a.db.get(Applicant, c.id).onboarding_token_hash}",
    }
    assert all(row.recipient == c.email and row.payload == {} for row in rows)


def test_intake_actual_old_new_payload_and_existing_placement_policy(admission_case):
    c = admission_case
    cohort = Cohort(tenant_id=c.a.tid, name="New cohort", discipline="fiber", status="active")
    track = Track(tenant_id=c.a.tid, name="New track", slug=uuid4().hex, status="active")
    c.a.db.add_all([cohort, track])
    c.a.db.flush()
    c.a.db.add(CohortTrack(tenant_id=c.a.tid, cohort_id=cohort.id, track_id=track.id, status="active"))
    c.a.db.flush()
    result = admissions.assign_web_applicant_intake(
        c.a.db, request=c.web, applicant_id=c.id, cohort_id=cohort.id, track_id=track.id, reason="Placement"
    )
    assert result.program == "New track" and result.cohort_id == cohort.id and result.track_id == track.id
    (event,) = _events(c)
    assert event.action == "applicant.intake_assigned" and event.details == {
        "from_cohort_id": str(c.cohort_id),
        "from_track_id": str(c.track_id),
        "to_cohort_id": str(cohort.id),
        "to_track_id": str(track.id),
        "track_name": "New track",
        "reason": "Placement",
        "source": "admin_web",
    }


@pytest.mark.parametrize("action", ["resend_invitation", "extend_access", "reset_assessment"])
def test_actual_invitation_access_reset_payload_and_false_delivery_is_accepted(admission_case, action):
    c = admission_case
    a = c.a.db.get(Applicant, c.id)
    if action == "reset_assessment":
        a.assessment_started_at = datetime.now(UTC)
        a.assessment_answers = {"old": ["A"]}
    else:
        entrance_exam.issue_token(c.a.db, applicant=a)
    a.assessment_deadline = datetime.now(UTC) + timedelta(days=1)
    # Empty historical recipient exercises actual enqueue_email(False), not a stub.
    a.email = ""
    c.a.db.flush()
    previous = a.assessment_deadline
    result = _review(c, action)
    (event,) = _events(c)
    assert event.actor_party_id == c.actors.admin.pid and event.is_success is None
    assert (
        event.action
        == {
            "resend_invitation": "applicant.invitation_resent",
            "extend_access": "applicant.access_extended",
            "reset_assessment": "applicant.assessment_reset",
        }[action]
    )
    expected = {"reason": "Reason", "source": "admin_web"}
    if action != "reset_assessment":
        expected.update(
            previous_assessment_deadline=previous.isoformat(),
            assessment_deadline=result.assessment_deadline.isoformat(),
        )
    if action == "resend_invitation":
        expected["extended_access"] = False
    assert event.details == expected
    assert c.a.db.scalars(select(EmailOutbox).where(*_outbox_scope(c))).all() == []


@pytest.mark.parametrize("transport", ["web", "api"])
@pytest.mark.parametrize("loss", ["expired", "revoked", "inactive", "suspended", "role"])
def test_actual_current_authority_refuses_and_preserves_all_effects(admission_case, transport, loss):
    c, actor = admission_case, admission_case.actors.admin
    before = _snapshot(c)
    if loss in {"expired", "revoked"}:
        values = (
            {"expires_at": datetime.now(UTC) - timedelta(seconds=1)}
            if loss == "expired"
            else {"revoked_at": datetime.now(UTC)}
        )
        c.a.admin.execute(update(AuthSession).where(AuthSession.id == actor.session_id).values(**values))
    elif loss == "inactive":
        c.a.admin.execute(update(Party).where(Party.id == actor.pid).values(is_active=False))
    elif loss == "suspended":
        c.a.admin.execute(
            update(AcademyAccountProfile).where(AcademyAccountProfile.party_id == actor.pid).values(status="suspended")
        )
    else:
        c.a.admin.execute(delete(PartyRoleGrant).where(PartyRoleGrant.party_id == actor.pid))
    c.a.admin.commit()
    with pytest.raises(HTTPException) as caught:
        _review(c) if transport == "web" else _transition(c, "screened")
    assert caught.value.status_code == (403 if loss == "role" else 303 if transport == "web" else 401)
    assert _snapshot(c) == before


def test_cross_tenant_and_unsupported_actor_source_never_gain_human_or_system_authority(admission_case, tenant_b):
    c = admission_case
    before = _snapshot(c)
    with pytest.raises(boundary.HumanOperationError):
        admissions.review_web_applicant(
            c.a.db, request=_request(c.actors, tenant=tenant_b), applicant_id=c.id, action="accept"
        )
    with pytest.raises(NotFoundError):
        admissions.review_web_applicant(c.a.db, request=c.web, applicant_id=uuid4(), action="accept")
    for kwargs in ({"actor_person_id": c.actors.admin.pid}, {"source": "onboarding"}, {"audit_context": object()}):
        with pytest.raises(TypeError):
            admissions.transition_api_applicant(
                c.a.db, request=c.api, applicant_id=c.id, to_status="screened", **kwargs
            )
    with pytest.raises(BadRequestError, match="genuine Request"):
        admissions.transition_applicant(
            c.a.db, applicant_id=c.id, to_status="screened", actor_person_id=c.actors.admin.pid
        )
    assert _snapshot(c) == before


@pytest.mark.parametrize("fault", ["omission", "copy", "repeat", "audit", "callback", "receipt", "no_audit"])
def test_private_refusals_are_sticky_and_caught_outer_commit_preserves_prior_work(admission_case, fault):
    c = admission_case
    before = _snapshot(c)
    c.a.db.get(Cohort, c.cohort_id).name = "Prior work"
    root = c.a.db.get_transaction()
    with pytest.raises(boundary.HumanOperationError):
        with boundary.transition_api_applicant(
            c.a.db, request=c.api, applicant_id=c.id, to_status="screened"
        ) as context:
            if fault == "omission":
                pass
            elif fault == "repeat":
                context.execute_admission()
                with pytest.raises(boundary.HumanOperationError):
                    context.execute_admission()
            elif fault == "copy":
                with pytest.raises(boundary.HumanOperationError):
                    copy(context).execute_admission()
            elif fault == "audit":
                with pytest.raises(boundary.HumanOperationError):
                    context.audit()
            else:
                with pytest.raises(boundary.HumanOperationError):
                    context.execute_admission(**{fault: None})
    assert context.__dict__ == {} and c.a.db.get_transaction() is root
    c.a.db.commit()
    resolve_database_runtime().set_tenant(c.a.db, c.a.tid)
    assert _snapshot(c) == before and c.a.db.get(Cohort, c.cohort_id).name == "Prior work"


@pytest.mark.parametrize("stage", ["audit", "tasks", "token", "outbox", "late_role", "transaction"])
def test_actual_late_domain_audit_and_outbox_failures_roll_back_whole_trace(admission_case, monkeypatch, stage):
    c = admission_case
    before = _snapshot(c)
    c.a.db.get(Cohort, c.cohort_id).name = "Prior work"
    same = learning.TransactionChanged("actual transaction guard")
    if stage == "audit":
        target, name = boundary, "write_audit_event"
    elif stage == "tasks":
        target, name = onboarding, "seed_tasks"
    elif stage == "token":
        target, name = admissions, "mint_onboarding_token"
    else:
        target, name = applicant_email, "send_onboarding_invite"
    actual = getattr(target, name)

    def interrupted(*args, **kwargs):
        result = actual(*args, **kwargs)
        if stage == "late_role":
            c.a.db.execute(delete(PartyRoleGrant).where(PartyRoleGrant.party_id == c.actors.admin.pid))
            return result
        if stage == "transaction":
            raise same
        raise RuntimeError("required real producer failed")

    monkeypatch.setattr(target, name, interrupted)
    with pytest.raises(
        HTTPException
        if stage == "late_role"
        else learning.TransactionChanged
        if stage == "transaction"
        else boundary.HumanOperationError
    ) as caught:
        _review(c)
    if stage == "transaction":
        assert caught.value is same
    c.a.db.commit()
    resolve_database_runtime().set_tenant(c.a.db, c.a.tid)
    assert _snapshot(c) == before and c.a.db.get(Cohort, c.cohort_id).name == "Prior work"


@pytest.mark.parametrize(
    "mutation",
    [
        "applicant",
        "audit_actor",
        "audit_time",
        "audit_success",
        "audit_delete",
        "task",
        "outbox",
        "outbox_key",
        "outbox_kind",
        "outbox_payload",
        "outbox_delete",
        "trace",
        "state",
    ],
)
def test_actual_persisted_result_receipts_tasks_outbox_and_trace_tamper_poisoned(admission_case, mutation):
    c = admission_case
    before = _snapshot(c)
    with pytest.raises(boundary.HumanOperationError):
        with boundary.review_web_applicant(c.a.db, request=c.web, applicant_id=c.id, action="accept") as context:
            result = context.execute_admission()
            event = context._admission.receipts[0][0]
            if mutation == "applicant":
                result.notes = "Substituted"
            elif mutation == "task":
                c.a.db.execute(update(OnboardingTask).where(OnboardingTask.applicant_id == c.id).values(status="done"))
            elif mutation.startswith("outbox"):
                row = c.a.db.scalars(select(EmailOutbox).where(*_outbox_scope(c))).one()
                assert row.payload == {} and row.recipient == c.email
                if mutation == "outbox_delete":
                    c.a.db.execute(delete(EmailOutbox).where(EmailOutbox.id == row.id))
                else:
                    values = {
                        "outbox": {"subject": "Substituted"},
                        "outbox_key": {"idempotency_key": "Substituted"},
                        "outbox_kind": {"kind": "Substituted"},
                        "outbox_payload": {"payload": {"substituted": True}},
                    }[mutation]
                    c.a.db.execute(update(EmailOutbox).where(EmailOutbox.id == row.id).values(**values))
            elif mutation == "trace":
                context._admission.receipts = ()
            elif mutation == "state":
                context._admission = copy(context._admission)
            elif mutation == "audit_delete":
                c.a.db.execute(delete(AuditEvent).where(AuditEvent.id == event.id))
            else:
                key, value = {
                    "audit_actor": ("actor_party_id", c.actors.instructor.pid),
                    "audit_time": ("occurred_at", datetime(2000, 1, 1, tzinfo=UTC)),
                    "audit_success": ("is_success", True),
                }[mutation]
                c.a.db.execute(update(AuditEvent).where(AuditEvent.id == event.id).values(**{key: value}))
            with pytest.raises((boundary.HumanOperationError, InvalidRequestError)):
                context.revalidate()
    c.a.db.commit()
    resolve_database_runtime().set_tenant(c.a.db, c.a.tid)
    assert _snapshot(c) == before


@pytest.mark.parametrize("existing", [False, True])
def test_real_human_enrollment_preserves_identity_credentials_roles_and_uses_learning(admission_case, existing):
    c = admission_case
    _review(c)
    for task in onboarding.list_tasks(c.a.db, tenant_id=c.a.tid, applicant_id=c.id):
        onboarding.set_task_status(c.a.db, task_id=task.id, status="done")
    person = (
        _person(c.a.db, tenant_id=c.a.tid, email=c.email, first_name="Existing", last_name="Person")
        if existing
        else None
    )
    before = c.a.db.get(Party, person.id).display_name if person else None
    if person:
        _credential(
            c.a.db,
            tenant_id=c.a.tid,
            person_id=person.id,
            email=person.email,
            password_hash=hash_password("preserved-password9"),
        )
        role = Role(tenant_id=c.a.tid, slug=f"preserved-{uuid4().hex}", name="Preserved")
        c.a.db.add(role)
        c.a.db.flush()
        c.a.db.add(PartyRoleGrant(tenant_id=c.a.tid, party_id=person.id, role_id=role.id))
        c.a.db.flush()
    credentials_before = (
        tuple(
            boundary._persisted_values(row)
            for row in c.a.db.scalars(select(UserCredential).where(UserCredential.party_id == person.id)).all()
        )
        if person
        else ()
    )
    grants_before = (
        tuple(
            boundary._persisted_values(row)
            for row in c.a.db.scalars(select(PartyRoleGrant).where(PartyRoleGrant.party_id == person.id)).all()
        )
        if person
        else ()
    )
    result = admissions.enroll_api_applicant(c.a.db, request=c.api, applicant_id=c.id, cohort_id=c.cohort_id)
    assert result.status == "enrolled" and result.person_id is not None
    (enrollment,) = c.a.db.scalars(select(Enrollment).where(Enrollment.person_id == result.person_id)).all()
    assert (enrollment.cohort_id, enrollment.track_id, enrollment.role_in_cohort, enrollment.status) == (
        c.cohort_id,
        c.track_id,
        "student",
        "active",
    )
    assert (
        tuple(
            boundary._persisted_values(row)
            for row in c.a.db.scalars(select(PartyRoleGrant).where(PartyRoleGrant.party_id == result.person_id)).all()
        )
        == grants_before
    )
    assert (
        tuple(
            boundary._persisted_values(row)
            for row in c.a.db.scalars(select(UserCredential).where(UserCredential.party_id == result.person_id)).all()
        )
        == credentials_before
    )
    if person:
        assert result.person_id == person.id and c.a.db.get(Party, person.id).display_name == before
    event = next(row for row in _events(c) if row.details["to_status"] == "enrolled")
    assert event.details == {"from_status": "onboarding", "to_status": "enrolled", "reason": "", "source": "admin_api"}
    assert event.actor_party_id == c.actors.admin.pid


@pytest.mark.parametrize("loss", ["expiry", "role_after_wait"])
def test_natural_applicant_row_wait_samples_auth_clock_after_wait_and_rolls_back(admission_case, monkeypatch, loss):
    c = admission_case
    if loss == "expiry":
        c.a.admin.execute(
            update(AuthSession)
            .where(AuthSession.id == c.actors.admin.session_id)
            .values(expires_at=datetime.now(UTC) + timedelta(seconds=2))
        )
        c.a.admin.commit()
    else:
        actual = boundary.write_audit_event

        def lose_role_after_real_audit(db, **kwargs):
            row = actual(db, **kwargs)
            db.execute(delete(PartyRoleGrant).where(PartyRoleGrant.party_id == c.actors.admin.pid))
            return row

        monkeypatch.setattr(boundary, "write_audit_event", lose_role_after_real_audit)
    c.a.db.commit()
    sessions = sessionmaker(bind=c.a.db.get_bind(), autoflush=False)
    locked, started, release = Event(), Event(), Event()
    backend = []

    def holder():
        with sessions() as db:
            resolve_database_runtime().set_tenant(db, c.a.tid)
            db.scalar(select(Applicant).where(Applicant.id == c.id).with_for_update())
            locked.set()
            assert release.wait(10)
            db.commit()

    def follower():
        with sessions() as db:
            resolve_database_runtime().set_tenant(db, c.a.tid)
            backend.append(db.scalar(text("SELECT pg_backend_pid()")))
            started.set()
            with pytest.raises(HTTPException) as caught:
                _transition(c, "screened", db=db)
            assert caught.value.status_code == (401 if loss == "expiry" else 403)
            db.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(holder)
        assert locked.wait(10)
        second = pool.submit(follower)
        assert started.wait(10)
        try:
            resolve_database_runtime().set_tenant(c.a.db, c.a.tid)
            _wait_for_lock(c.a.db, backend[0])
            if loss == "expiry":
                Event().wait(2.1)
        finally:
            release.set()
        first.result(timeout=20)
        second.result(timeout=20)
    c.a.db.expire_all()
    assert c.a.db.get(Applicant, c.id).status == "applied" and _events(c) == []


def test_request_tenant_cannot_mutate_an_existing_other_tenant_applicant(admission_case, tenant_b):
    c = admission_case
    other = Applicant(
        tenant_id=tenant_b.id,
        email=f"other-{uuid4().hex}@b.edu",
        first_name="Other",
        last_name="Tenant",
        status="applied",
    )
    c.a.admin.add(other)
    c.a.admin.commit()
    before = other.status
    with pytest.raises(NotFoundError):
        admissions.transition_api_applicant(c.a.db, request=c.api, applicant_id=other.id, to_status="screened")
    c.a.db.commit()
    resolve_database_runtime().set_tenant(c.a.db, c.a.tid)
    c.a.admin.refresh(other)
    assert other.status == before and _events(c) == []


def test_missing_issuer_and_state_copy_cannot_claim_private_execution(admission_case):
    c = admission_case
    with pytest.raises(boundary.HumanOperationError):
        with boundary.transition_api_applicant(
            c.a.db, request=c.api, applicant_id=c.id, to_status="screened"
        ) as context:
            context._admission = copy(context._admission)
            with pytest.raises(boundary.HumanOperationError):
                context.execute_admission()
    assert context.__dict__ == {}
    with pytest.raises(boundary.HumanOperationError):
        context.execute_admission()
    assert context.__dict__ == {} and _events(c) == []


@pytest.mark.parametrize("race", ["existing_email", "new_identity"])
def test_natural_party_row_wait_rejects_changed_exact_email_membership(admission_case, race):
    c = admission_case
    _review(c)
    for task in onboarding.list_tasks(c.a.db, tenant_id=c.a.tid, applicant_id=c.id):
        onboarding.set_task_status(c.a.db, task_id=task.id, status="done")
    c.a.db.commit()
    person = (
        _person(c.a.admin, tenant_id=c.a.tid, email=c.email, first_name="Existing", last_name="Person")
        if race == "existing_email"
        else None
    )
    c.a.admin.commit()
    resolve_database_runtime().set_tenant(c.a.db, c.a.tid)
    before = _snapshot(c)
    c.a.db.commit()
    sessions = sessionmaker(bind=c.a.db.get_bind(), autoflush=False)
    locked, started, release = Event(), Event(), Event()
    backend = []

    def holder():
        with sessions() as db:
            resolve_database_runtime().set_tenant(db, c.a.tid)
            row = db.scalar(
                select(Party).where(Party.id == (person.id if person else c.actors.admin.pid)).with_for_update()
            )
            locked.set()
            assert release.wait(10)
            if person:
                row.email = f"moved-{uuid4().hex}@a.edu"
            else:
                _person(db, tenant_id=c.a.tid, email=c.email, first_name="Concurrent", last_name="Person")
            db.commit()

    def follower():
        with sessions() as db:
            resolve_database_runtime().set_tenant(db, c.a.tid)
            backend.append(db.scalar(text("SELECT pg_backend_pid()")))
            started.set()
            with pytest.raises(ConflictError):
                admissions.enroll_api_applicant(db, request=c.api, applicant_id=c.id, cohort_id=c.cohort_id)
            db.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(holder)
        assert locked.wait(10)
        second = pool.submit(follower)
        assert started.wait(10)
        try:
            resolve_database_runtime().set_tenant(c.a.db, c.a.tid)
            _wait_for_lock(c.a.db, backend[0])
        finally:
            release.set()
        first.result(timeout=20)
        second.result(timeout=20)
    assert _snapshot(c) == before


@pytest.mark.parametrize("changed", [False, True])
def test_failure_after_actual_learning_enrollment_rolls_back_new_identity_and_enrollment(
    admission_case, monkeypatch, changed
):
    c = admission_case
    _review(c)
    for task in onboarding.list_tasks(c.a.db, tenant_id=c.a.tid, applicant_id=c.id):
        onboarding.set_task_status(c.a.db, task_id=task.id, status="done")
    c.a.db.flush()
    before = _snapshot(c)
    parties = set(c.a.db.scalars(select(Party.id)).all())
    c.a.db.get(Cohort, c.cohort_id).name = "Prior enrollment work"
    root = c.a.db.get_transaction()
    same = learning.TransactionChanged("actual enrollment transaction guard")
    actual = learning.ensure_admission_enrollment

    def interrupted(db, command, **kwargs):
        actual(db, command, **kwargs)
        if changed:
            raise same
        raise RuntimeError("real enrollment failed after DML")

    monkeypatch.setattr(learning, "ensure_admission_enrollment", interrupted)
    with pytest.raises(learning.TransactionChanged if changed else boundary.HumanOperationError) as caught:
        admissions.enroll_api_applicant(c.a.db, request=c.api, applicant_id=c.id, cohort_id=c.cohort_id)
    if changed:
        assert caught.value is same
    assert c.a.db.get_transaction() is root
    c.a.db.commit()
    resolve_database_runtime().set_tenant(c.a.db, c.a.tid)
    assert _snapshot(c) == before and set(c.a.db.scalars(select(Party.id)).all()) == parties
    assert c.a.db.get(Cohort, c.cohort_id).name == "Prior enrollment work"
    assert c.a.db.scalars(select(Enrollment).where(Enrollment.cohort_id == c.cohort_id)).all() == []
