"""Onboarding workflow: seeding on accept, completion, and the enrolment gate."""

from __future__ import annotations

import inspect
import uuid

import pytest
from dotmac_learning import EnsureAdmissionEnrollmentCommand
from dotmac_learning.result_contracts import TransactionChanged

from app.models.admissions import Applicant
from app.models.cohort import Cohort
from app.models.track import CohortTrack, Track
from app.services import admissions, onboarding
from app.services import invitation_issuance as issuance
from app.services.exceptions import BadRequestError
from app.services.security import hash_password
from legacy_probe.services.admission_request_fixtures import admission_admin_request
from legacy_probe.services.test_canonical_invitation_issuance import _credential, _person
from legacy_probe.services.test_canonical_onboarding import _finish, onboarding_case


def _accepted_applicant(admin_session, tenant):
    cohort = Cohort(
        tenant_id=tenant.id,
        name=f"Onboarding-{uuid.uuid4().hex[:6]}",
        discipline="fiber",
        status="active",
    )
    track = Track(
        tenant_id=tenant.id,
        slug=f"onboarding-{uuid.uuid4().hex[:6]}",
        name="Fiber",
        status="active",
    )
    admin_session.add_all([cohort, track])
    admin_session.flush()
    admin_session.add(
        CohortTrack(
            tenant_id=tenant.id,
            cohort_id=cohort.id,
            track_id=track.id,
            status="active",
        )
    )
    admin_session.flush()
    a = Applicant(
        tenant_id=tenant.id,
        email=f"o{uuid.uuid4().hex[:6]}@a.ex",
        first_name="On",
        last_name="Board",
        status="accepted",
        cohort_id=cohort.id,
        track_id=track.id,
        program=track.name,
    )
    admin_session.add(a)
    admin_session.flush()
    return a


def test_transition_to_onboarding_seeds_tasks(admin_session, tenant_a):
    actor_request = admission_admin_request(admin_session, tenant_a)
    a = _accepted_applicant(admin_session, tenant_a)
    admissions.transition_api_applicant(admin_session, request=actor_request, applicant_id=a.id, to_status="onboarding")
    tasks = onboarding.list_tasks(admin_session, tenant_id=tenant_a.id, applicant_id=a.id)
    assert [t.key for t in tasks] == [k for k, _ in onboarding.onboarding_policy(admin_session).task_catalog]
    assert all(t.status == "pending" for t in tasks)


def test_seed_is_idempotent(admin_session, tenant_a):
    a = _accepted_applicant(admin_session, tenant_a)
    onboarding.seed_tasks(admin_session, tenant_id=tenant_a.id, applicant_id=a.id)
    onboarding.seed_tasks(admin_session, tenant_id=tenant_a.id, applicant_id=a.id)
    tasks = onboarding.list_tasks(admin_session, tenant_id=tenant_a.id, applicant_id=a.id)
    assert len(tasks) == len(onboarding.onboarding_policy(admin_session).task_catalog)


def test_is_complete_tracks_task_status(admin_session, tenant_a):
    a = _accepted_applicant(admin_session, tenant_a)
    onboarding.seed_tasks(admin_session, tenant_id=tenant_a.id, applicant_id=a.id)
    assert onboarding.is_complete(admin_session, tenant_id=tenant_a.id, applicant_id=a.id) is False
    for t in onboarding.list_tasks(admin_session, tenant_id=tenant_a.id, applicant_id=a.id):
        onboarding.set_task_status(admin_session, task_id=t.id, status="done")
    assert onboarding.is_complete(admin_session, tenant_id=tenant_a.id, applicant_id=a.id) is True


def test_enroll_blocked_until_onboarding_complete(admin_session, tenant_a):
    actor_request = admission_admin_request(admin_session, tenant_a)
    a = _accepted_applicant(admin_session, tenant_a)
    admissions.transition_api_applicant(admin_session, request=actor_request, applicant_id=a.id, to_status="onboarding")
    cohort = admin_session.get(Cohort, a.cohort_id)

    with pytest.raises(BadRequestError):
        admissions.enroll_api_applicant(admin_session, request=actor_request, applicant_id=a.id, cohort_id=cohort.id)

    for t in onboarding.list_tasks(admin_session, tenant_id=tenant_a.id, applicant_id=a.id):
        onboarding.set_task_status(admin_session, task_id=t.id, status="done")
    result = admissions.enroll_api_applicant(
        admin_session, request=actor_request, applicant_id=a.id, cohort_id=cohort.id
    )
    assert result.status == "enrolled"


def test_complete_task_by_key(admin_session, tenant_a):
    a = _accepted_applicant(admin_session, tenant_a)
    onboarding.seed_tasks(admin_session, tenant_id=tenant_a.id, applicant_id=a.id)
    t = onboarding.complete_task_by_key(
        admin_session, tenant_id=tenant_a.id, applicant_id=a.id, key="entrance_assessment"
    )
    assert t is not None and t.status == "done"
    # Unknown key is a no-op, not an error.
    assert onboarding.complete_task_by_key(admin_session, tenant_id=tenant_a.id, applicant_id=a.id, key="nope") is None


# These scenarios now use genuine persisted canonical onboarding state. Earlier
# admin audit bridges remain prerequisites for explicit administrative success.

assert onboarding_case


@pytest.mark.parametrize("new_person", [False, True])
@pytest.mark.parametrize("admin_actor", [False, True])
def test_admission_delegates_same_session_command_after_product_guards_and_person(
    onboarding_case, monkeypatch, new_person, admin_actor
):
    c = onboarding_case
    db = c.a.db
    person = (
        None if new_person else _person(db, tenant_id=c.a.tid, email=c.email, first_name="Existing", last_name="Name")
    )
    calls = []
    owner = admissions.learning.ensure_admission_enrollment

    def record(session, command, *, initial_state):
        assert session is db and type(command) is EnsureAdmissionEnrollmentCommand
        assert command.tenant_id == c.a.tid and command.cohort_id == c.cohort_id and command.track_id == c.track_id
        if person is not None:
            assert command.person_id == person.id
        applicant = db.get(admissions.Applicant, c.id)
        assert applicant.person_id is None and applicant.status == "onboarding"
        calls.append(command)
        return owner(session, command, initial_state=initial_state)

    monkeypatch.setattr(admissions.learning, "ensure_admission_enrollment", record)
    if admin_actor:
        request = admission_admin_request(db, c.a.tenant)
        result = admissions.enroll_api_applicant(db, request=request, applicant_id=c.id, cohort_id=c.cohort_id)
    else:
        # Actual private bearer preserves the genuine system/null-human origin.
        result = _finish(c).applicant
    assert result.status == "enrolled" and result.person_id == calls[0].person_id
    assert len(calls) == 1
    if person is not None:
        assert result.person_id == person.id
    else:
        from dotmac_kernel.models import Party, PartyPerson

        from app.models.identity_extensions import AcademyAccountProfile

        party = db.get(Party, result.person_id)
        subtype = db.get(PartyPerson, result.person_id)
        profile = db.get(AcademyAccountProfile, result.person_id)
        assert (party.tenant_id, party.email, subtype.first_name, subtype.last_name) == (
            c.a.tid,
            c.email,
            result.first_name,
            result.last_name,
        )
        assert party.display_name == f"{result.first_name} {result.last_name}"
        assert profile.tenant_id == c.a.tid and profile.party_id == party.id


@pytest.mark.parametrize("guard", ["status", "tasks", "cohort", "intake"])
def test_admission_error_priority_precedes_person_and_learning(onboarding_case, monkeypatch, guard):
    c = onboarding_case
    actor_request = admission_admin_request(c.a.db, c.a.tenant)
    db, applicant = c.a.db, c.a.db.get(admissions.Applicant, c.id)
    cohort = c.cohort_id
    if guard == "status":
        applicant.status = "applied"
        db.flush()
    if guard in {"status", "tasks"}:
        task = onboarding.list_tasks(db, tenant_id=c.a.tid, applicant_id=c.id)[0]
        onboarding.set_task_status(db, task_id=task.id, status="pending")
    if guard in {"status", "tasks", "cohort"}:
        cohort = uuid.uuid4()
    # Plant invalid intake for EVERY case: each earlier guard must win while
    # status/tasks/cohort/intake are simultaneously invalid as applicable.
    applicant.track_id = None
    db.flush()

    def forbidden(*args, **kwargs):
        pytest.fail("learning must follow all product guards")

    from app.services import admission_enrollment_policy

    monkeypatch.setattr(admission_enrollment_policy, "admission_enrollment_policy", forbidden)
    monkeypatch.setattr(admissions.learning, "ensure_admission_enrollment", forbidden)
    messages = {
        "status": "must be in 'onboarding'",
        "tasks": "outstanding onboarding tasks",
        "cohort": "assigned cohort",
        "intake": "no active canonical training track",
    }
    with pytest.raises(BadRequestError, match=messages[guard]):
        admissions.enroll_api_applicant(db, request=actor_request, applicant_id=c.id, cohort_id=cohort)
    assert applicant.person_id is None and applicant.status != "enrolled"
    from dotmac_kernel.models import Party
    from sqlalchemy import select

    assert db.scalars(select(Party).where(Party.tenant_id == c.a.tid, Party.email == c.email)).first() is None


@pytest.mark.parametrize("error_type", [RuntimeError, ValueError, TransactionChanged])
def test_admission_owner_failure_propagates_before_link_and_transition(onboarding_case, monkeypatch, error_type):
    c = onboarding_case
    failure = error_type("owner refusal")

    def owner(session, command, *, initial_state):
        assert session is c.a.db and command.tenant_id == c.a.tid
        raise failure

    monkeypatch.setattr(admissions.learning, "ensure_admission_enrollment", owner)
    with pytest.raises(error_type) as caught:
        _finish(c)
    assert caught.value is failure
    applicant = c.a.db.get(admissions.Applicant, c.id)
    assert applicant.person_id is None and applicant.status == "onboarding"


@pytest.mark.parametrize("has_credential", [False, True])
def test_auto_enrollment_keeps_admission_role_credential_invite_order(onboarding_case, monkeypatch, has_credential):
    from app.services import canonical_identity_store as store

    c = onboarding_case
    person = _person(c.a.db, tenant_id=c.a.tid, email=c.email, first_name="Existing", last_name="Name")
    if has_credential:
        _credential(
            c.a.db,
            tenant_id=c.a.tid,
            person_id=person.id,
            email="alternate@a.edu",
            password_hash=hash_password("chosen-password9"),
        )
    events = []
    for module, name, event in [
        (admissions.learning, "ensure_admission_enrollment", "learning"),
        (store, "ensure_grant", "role"),
        (store, "credential_member_ids", "credential"),
        (issuance, "_issue_invitation_token", "invite"),
    ]:
        original = getattr(module, name)

        def record(*args, _original=original, _event=event, **kwargs):
            events.append(_event)
            return _original(*args, **kwargs)

        monkeypatch.setattr(module, name, record)
    result = _finish(c)
    assert result.enrolled_now and bool(result.invite_raw) is not has_credential
    assert events.index("learning") < events.index("role") < events.index("credential")
    assert ("invite" in events) is not has_credential
    assert not _finish(c).enrolled_now
    assert events.count("learning") == 1


@pytest.mark.parametrize("reason", ["status", "cohort", "tasks"])
def test_auto_enrollment_noop_never_reaches_admission_owner(onboarding_case, monkeypatch, reason):
    c = onboarding_case
    applicant = c.a.db.get(admissions.Applicant, c.id)
    if reason == "status":
        applicant.status = "enrolled"
    elif reason == "cohort":
        applicant.cohort_id = None
    else:
        task = next(
            t for t in onboarding.list_tasks(c.a.db, tenant_id=c.a.tid, applicant_id=c.id) if t.key != "orientation"
        )
        onboarding.set_task_status(c.a.db, task_id=task.id, status="pending")
    c.a.db.flush()

    def forbidden(*args, **kwargs):
        pytest.fail("no-op must not call admission owner")

    from app.services import admission_enrollment_policy

    monkeypatch.setattr(admission_enrollment_policy, "admission_enrollment_policy", forbidden)
    monkeypatch.setattr(admissions.learning, "ensure_admission_enrollment", forbidden)
    result = _finish(c)
    assert not result.enrolled_now and result.invite_raw is None


@pytest.mark.parametrize("existing_track", ["absent", "same", "different"])
def test_admission_existing_enrollment_preserves_state_and_changes_only_track(
    admin_session, tenant_a, existing_track, monkeypatch
):
    """Real Academy lineage canary; canonical adoption is required before running.

    No synthetic Kernel Party is manufactured here. This canary and the five
    retained DB tests require the future canonical fixture/migration assembly.
    """
    from sqlalchemy import select

    from app.models.cohort import Enrollment

    actor_request = admission_admin_request(admin_session, tenant_a)
    applicant = _accepted_applicant(admin_session, tenant_a)
    admissions.transition_api_applicant(
        admin_session, request=actor_request, applicant_id=applicant.id, to_status="onboarding"
    )
    for task in onboarding.list_tasks(admin_session, tenant_id=tenant_a.id, applicant_id=applicant.id):
        onboarding.set_task_status(admin_session, task_id=task.id, status="done")
    person = _person(
        admin_session, tenant_id=tenant_a.id, email=applicant.email, first_name="Existing", last_name="Person"
    )
    if existing_track != "absent":
        row = Enrollment(
            tenant_id=tenant_a.id,
            cohort_id=applicant.cohort_id,
            person_id=person.id,
            track_id=applicant.track_id if existing_track == "same" else None,
            role_in_cohort="instructor",
            status="dropped",
            audience="staff",
            employee_ref="Employee-1",
        )
        admin_session.add(row)
        admin_session.flush()
        original_id = row.id
    flush_callers = []
    original_flush = admin_session.flush

    def record_flush(*args, **kwargs):
        flush_callers.append(inspect.currentframe().f_back.f_code.co_name)
        return original_flush(*args, **kwargs)

    monkeypatch.setattr(admin_session, "flush", record_flush)
    result = admissions.enroll_api_applicant(
        admin_session, request=actor_request, applicant_id=applicant.id, cohort_id=applicant.cohort_id
    )
    rows = admin_session.scalars(
        select(Enrollment).where(
            Enrollment.tenant_id == tenant_a.id,
            Enrollment.cohort_id == applicant.cohort_id,
            Enrollment.person_id == person.id,
        )
    ).all()
    assert result is applicant and result.person_id == person.id and result.status == "enrolled"
    assert len(rows) == 1 and rows[0].track_id == applicant.track_id
    assert flush_callers.count("ensure_admission_enrollment") == (0 if existing_track == "same" else 1)
    if existing_track == "absent":
        assert (rows[0].role_in_cohort, rows[0].status, rows[0].audience) == ("student", "active", None)
    else:
        assert rows[0].id == original_id
        assert (rows[0].role_in_cohort, rows[0].status, rows[0].audience, rows[0].employee_ref) == (
            "instructor",
            "dropped",
            "staff",
            "Employee-1",
        )


@pytest.mark.parametrize("new_person", [False, True])
def test_bearer_admission_delegates_same_session_command_additively(onboarding_case, monkeypatch, new_person):
    c = onboarding_case
    person = (
        None
        if new_person
        else _person(c.a.db, tenant_id=c.a.tid, email=c.email, first_name="Existing", last_name="Name")
    )
    calls = []
    owner = admissions.learning.ensure_admission_enrollment

    def record(session, command, *, initial_state):
        assert session is c.a.db and type(command) is EnsureAdmissionEnrollmentCommand
        assert command.model_dump() == {
            "tenant_id": c.a.tid,
            "cohort_id": c.cohort_id,
            "person_id": command.person_id if person is None else person.id,
            "track_id": c.track_id,
        }
        applicant = session.get(admissions.Applicant, c.id)
        assert applicant.person_id is None and applicant.status == "onboarding"
        calls.append(command)
        return owner(session, command, initial_state=initial_state)

    monkeypatch.setattr(admissions.learning, "ensure_admission_enrollment", record)
    result = _finish(c).applicant
    assert len(calls) == 1 and result.status == "enrolled" and result.person_id == calls[0].person_id
    if new_person:
        from dotmac_kernel.models import Party, PartyPerson

        party, subtype = c.a.db.get(Party, result.person_id), c.a.db.get(PartyPerson, result.person_id)
        assert (party.tenant_id, party.email, subtype.first_name, subtype.last_name) == (
            c.a.tid,
            c.email,
            result.first_name,
            result.last_name,
        )


@pytest.mark.parametrize("error_type", [RuntimeError, ValueError, TransactionChanged])
def test_bearer_admission_owner_failure_remains_additive(onboarding_case, monkeypatch, error_type):
    c = onboarding_case
    failure = error_type("owner refusal")

    def owner(session, command, *, initial_state):
        assert session is c.a.db and command.tenant_id == c.a.tid
        raise failure

    monkeypatch.setattr(admissions.learning, "ensure_admission_enrollment", owner)
    with pytest.raises(error_type) as caught:
        _finish(c)
    assert caught.value is failure
    applicant = c.a.db.get(admissions.Applicant, c.id)
    assert applicant.person_id is None and applicant.status == "onboarding"
