"""Original invitation scenarios ported to genuine canonical cookie authority.

Authored NOT RUN. Every old scenario/assertion is retained except explicitly
recorded mutable-Person and mock-transaction trace normalization. No fakePerson,
authority boolean, skip, schema shim or admin business writer is introduced.
"""

from __future__ import annotations

import pytest
from dotmac_kernel.models import Party, UserCredential
from dotmac_learning import InvitationEnrollmentInitialState
from dotmac_learning.participation_models import Enrollment
from sqlalchemy import func, select

from app.models.account_token import AccountToken
from app.services.account_invitations import CohortAssignment
from app.services.invitation_policy import invitation_enrollment_policy
from app.services.security import hash_password
from legacy_probe.services.test_canonical_invitation_issuance import (
    _apply,
    _cohort,
    _credential,
    _curriculum,
    _person,
    _run,
    _set_actor_role,
    issuer,
)

assert issuer


def test_new_user_invitation_is_canonical_and_enrolled(issuer):
    admin_session, tenant_a = (issuer.db, issuer.tenant)
    cohort = _cohort(admin_session, tenant_a.id)
    result = _run(
        issuer,
        email="  NEW.Student@Example.COM ",
        first_name="New",
        last_name="Student",
        role="student",
        assignments=(CohortAssignment(cohort_id=cohort.id),),
    )
    assert result.created_person is True
    assert result.had_credential is False
    assert result.token is not None
    assert result.person.email == "new.student@example.com"
    assert (
        admin_session.scalar(
            select(func.count())
            .select_from(Enrollment)
            .where(Enrollment.person_id == result.person.id)
            .where(Enrollment.cohort_id == cohort.id)
        )
        == 1
    )
    admin_session.rollback()


def test_existing_user_without_credentials_is_reinvited_idempotently(issuer):
    admin_session, tenant_a = (issuer.db, issuer.tenant)
    cohort = _cohort(admin_session, tenant_a.id)
    person = _person(
        admin_session, tenant_id=tenant_a.id, email="pending@example.com", first_name="Pending", last_name="Student"
    )
    admin_session.flush()
    first = _run(
        issuer,
        email=" PENDING@EXAMPLE.COM ",
        first_name="Ignored",
        last_name="Ignored",
        role="student",
        assignments=(CohortAssignment(cohort_id=cohort.id),),
    )
    second = _run(
        issuer,
        email="pending@example.com",
        first_name="Ignored",
        last_name="Ignored",
        role="student",
        assignments=(CohortAssignment(cohort_id=cohort.id),),
    )
    assert first.person.id == person.id == second.person.id
    assert first.token is not None and second.token is not None and (first.token != second.token)
    assert (
        admin_session.scalar(
            select(func.count())
            .select_from(Party)
            .where(Party.tenant_id == tenant_a.id)
            .where(func.lower(func.btrim(Party.email)) == "pending@example.com")
        )
        == 1
    )
    assert (
        admin_session.scalar(
            select(func.count())
            .select_from(Enrollment)
            .where(Enrollment.person_id == person.id)
            .where(Enrollment.cohort_id == cohort.id)
        )
        == 1
    )
    tokens = admin_session.scalars(
        select(AccountToken)
        .where(AccountToken.person_id == person.id)
        .where(AccountToken.kind == "invite")
        .order_by(AccountToken.created_at)
    ).all()
    assert len(tokens) == 2
    assert sum((token.used_at is None for token in tokens)) == 1
    admin_session.rollback()


def test_existing_user_with_credentials_is_enrolled_without_activation(issuer):
    admin_session, tenant_a = (issuer.db, issuer.tenant)
    cohort = _cohort(admin_session, tenant_a.id)
    person = _person(
        admin_session, tenant_id=tenant_a.id, email="member@example.com", first_name="Existing", last_name="Member"
    )
    admin_session.flush()
    admin_session.add(
        _credential(
            admin_session,
            tenant_id=tenant_a.id,
            person_id=person.id,
            email="member@example.com",
            password_hash=hash_password("existing-password"),
        )
    )
    admin_session.flush()
    result = _run(
        issuer,
        email=" MEMBER@example.com ",
        first_name="Existing",
        last_name="Member",
        role="student",
        assignments=(CohortAssignment(cohort_id=cohort.id),),
    )
    repeated = _run(
        issuer,
        email="member@example.com",
        first_name="Existing",
        last_name="Member",
        role="student",
        assignments=(CohortAssignment(cohort_id=cohort.id),),
    )
    assert result.person.id == person.id == repeated.person.id
    assert result.had_credential is True
    assert result.token is None and repeated.token is None
    assert (
        admin_session.scalar(
            select(func.count())
            .select_from(Enrollment)
            .where(Enrollment.person_id == person.id)
            .where(Enrollment.cohort_id == cohort.id)
        )
        == 1
    )
    assert (
        admin_session.scalar(
            select(func.count())
            .select_from(AccountToken)
            .where(AccountToken.person_id == person.id)
            .where(AccountToken.kind == "invite")
        )
        == 0
    )
    admin_session.rollback()


def test_already_enrolled_user_is_reactivated_without_duplicate(issuer):
    admin_session, tenant_a = (issuer.db, issuer.tenant)
    _set_actor_role(issuer, "instructor")
    cohort = _cohort(admin_session, tenant_a.id)
    person = _person(
        admin_session, tenant_id=tenant_a.id, email="returning@example.com", first_name="Returning", last_name="Student"
    )
    admin_session.flush()
    enrollment = Enrollment(
        tenant_id=tenant_a.id, cohort_id=cohort.id, person_id=person.id, role_in_cohort="student", status="dropped"
    )
    admin_session.add(enrollment)
    admin_session.flush()
    _run(
        issuer,
        email=person.email,
        first_name=person.first_name,
        last_name=person.last_name,
        role="student",
        assignments=(CohortAssignment(cohort_id=cohort.id),),
    )
    assert enrollment.status == "active"
    assert (
        admin_session.scalar(
            select(func.count())
            .select_from(Enrollment)
            .where(Enrollment.person_id == person.id)
            .where(Enrollment.cohort_id == cohort.id)
        )
        == 1
    )
    admin_session.rollback()


def test_reactivation_description_names_the_prior_status(issuer):
    """The description string _apply_assignment returns must distinguish a
    reactivation from a genuinely new enrollment — this is what makes the
    reactivation visible on the invite_to_cohort web surface at all."""
    admin_session, tenant_a = (issuer.db, issuer.tenant)
    _set_actor_role(issuer, "instructor")
    cohort = _cohort(admin_session, tenant_a.id)
    person = _person(
        admin_session,
        tenant_id=tenant_a.id,
        email="was-waitlisted@example.com",
        first_name="Was",
        last_name="Waitlisted",
    )
    admin_session.flush()
    enrollment = Enrollment(
        tenant_id=tenant_a.id, cohort_id=cohort.id, person_id=person.id, role_in_cohort="student", status="waitlisted"
    )
    admin_session.add(enrollment)
    admin_session.flush()
    result = _run(
        issuer,
        email=person.email,
        first_name=person.first_name,
        last_name=person.last_name,
        role="student",
        assignments=(CohortAssignment(cohort_id=cohort.id),),
    )
    assert any(("reactivated (was waitlisted)" in item for item in result.assignments))
    admin_session.rollback()


def test_invite_and_enroll_never_downgrades_an_existing_instructor_to_student(issuer):
    """Fix 2: /invite and /enroll (and the admin Users page's invite path,
    which shares this same function) must never silently demote an existing
    cohort instructor to student — unconditionally, no opt-out."""
    admin_session, tenant_a = (issuer.db, issuer.tenant)
    _set_actor_role(issuer, "instructor")
    cohort = _cohort(admin_session, tenant_a.id)
    person = _person(
        admin_session,
        tenant_id=tenant_a.id,
        email="instructor@example.com",
        first_name="Existing",
        last_name="Instructor",
    )
    admin_session.flush()
    enrollment = Enrollment(
        tenant_id=tenant_a.id, cohort_id=cohort.id, person_id=person.id, role_in_cohort="instructor", status="active"
    )
    admin_session.add(enrollment)
    admin_session.flush()
    result = _run(
        issuer,
        email=person.email,
        first_name=person.first_name,
        last_name=person.last_name,
        role="student",
        assignments=(CohortAssignment(cohort_id=cohort.id),),
    )
    assert enrollment.role_in_cohort == "instructor"
    assert enrollment.status == "active"
    assert any(("already an instructor, role unchanged" in item for item in result.assignments))
    admin_session.rollback()


def test_apply_assignment_direct_call_preserves_instructor_role(issuer):
    """Same guarantee exercised directly against _apply_assignment (the single
    function both the instructor route and the admin Users route share).

    actor_is_admin=True here: this test specifically checks that an ADMIN
    preserves the instructor role while reactivating a dropped instructor
    enrollment — that is still the correct, allowed behavior. A non-admin
    performing this same scenario is refused; see the sibling test below.
    """
    admin_session, tenant_a = (issuer.db, issuer.tenant)
    cohort = _cohort(admin_session, tenant_a.id)
    person = _person(
        admin_session,
        tenant_id=tenant_a.id,
        email="direct-instructor@example.com",
        first_name="Direct",
        last_name="Instructor",
    )
    admin_session.flush()
    enrollment = Enrollment(
        tenant_id=tenant_a.id, cohort_id=cohort.id, person_id=person.id, role_in_cohort="instructor", status="dropped"
    )
    admin_session.add(enrollment)
    admin_session.flush()
    descriptions = _apply(
        issuer,
        person=person,
        role="student",
        assignment=CohortAssignment(cohort_id=cohort.id),
        role_policy=invitation_enrollment_policy(admin_session),
    )
    assert enrollment.role_in_cohort == "instructor"
    assert enrollment.status == "active"
    assert any(("already an instructor, role unchanged" in item for item in descriptions))
    admin_session.rollback()


def test_apply_assignment_direct_call_refuses_non_admin_reactivating_instructor(issuer):
    """The mirror of the test above: a non-admin actor may NOT reactivate a
    dropped instructor-role enrollment through _apply_assignment — this is
    exactly the privilege-restoration gap this fix closes. The enrollment's
    status must be left unchanged (still "dropped") after the refusal."""
    admin_session, tenant_a = (issuer.db, issuer.tenant)
    _set_actor_role(issuer, "instructor")
    from app.services.exceptions import BadRequestError

    cohort = _cohort(admin_session, tenant_a.id)
    person = _person(
        admin_session,
        tenant_id=tenant_a.id,
        email="non-admin-instructor@example.com",
        first_name="NonAdmin",
        last_name="Instructor",
    )
    admin_session.flush()
    enrollment = Enrollment(
        tenant_id=tenant_a.id, cohort_id=cohort.id, person_id=person.id, role_in_cohort="instructor", status="dropped"
    )
    admin_session.add(enrollment)
    admin_session.flush()
    with pytest.raises(BadRequestError):
        _apply(
            issuer,
            person=person,
            role="student",
            assignment=CohortAssignment(cohort_id=cohort.id),
            role_policy=invitation_enrollment_policy(admin_session),
        )
    assert enrollment.role_in_cohort == "instructor"
    assert enrollment.status == "dropped"
    admin_session.rollback()


def test_apply_assignment_still_sets_role_for_a_genuinely_new_student(issuer):
    """Sanity check for the fix's near-miss: a brand-new enrollment (no
    existing instructor role) still gets member_role set normally."""
    admin_session, tenant_a = (issuer.db, issuer.tenant)
    _set_actor_role(issuer, "instructor")
    cohort = _cohort(admin_session, tenant_a.id)
    person = _person(
        admin_session, tenant_id=tenant_a.id, email="brand-new@example.com", first_name="Brand", last_name="New"
    )
    admin_session.flush()
    descriptions = _apply(
        issuer,
        person=person,
        role="student",
        assignment=CohortAssignment(cohort_id=cohort.id),
        role_policy=invitation_enrollment_policy(admin_session),
    )
    admin_session.flush()
    enrollment = admin_session.scalars(
        select(Enrollment).where(Enrollment.cohort_id == cohort.id).where(Enrollment.person_id == person.id)
    ).first()
    assert enrollment.role_in_cohort == "student"
    assert descriptions == [f"{cohort.name} cohort as student"]
    admin_session.rollback()


@pytest.mark.parametrize("actor_is_admin", [True, False])
@pytest.mark.parametrize("preserved,previous", [(False, None), (False, "waitlisted"), (True, "dropped")])
def test_assignment_delegates_identical_intent_and_descriptions(
    issuer, monkeypatch, actor_is_admin, preserved, previous
):
    from types import SimpleNamespace

    from app.services import account_invitations as service
    from app.services.invitation_policy import AcademyInvitationEnrollmentPolicy

    _set_actor_role(issuer, "admin" if actor_is_admin else "instructor")
    db, tenant_id = issuer.db, issuer.tid
    person = _person(db, tenant_id=tenant_id, email="delegation@a.edu", first_name="Real", last_name="Target")
    cohort = _cohort(db, tenant_id, "Cohort")
    track, a, b = _curriculum(db, tenant_id, cohort)
    policy = AcademyInvitationEnrollmentPolicy({"custom": "instructor"}, "student", InvitationEnrollmentInitialState(status="active"))
    calls = []

    def owner(session, command, *, authority, role_policy, initial_state):
        assert initial_state is policy.initial_state
        assert initial_state.status == "active"
        calls.append((session, command, authority, role_policy))
        activation = None if previous is None else SimpleNamespace(outcome="reactivated", previous_status=previous)
        return SimpleNamespace(member_role="student", preserved_instructor_role=preserved, activation=activation)

    monkeypatch.setattr(service.learning, "ensure_invitation_enrollment", owner)
    global_role = "opaque-custom-global-role" if actor_is_admin else "student"
    descriptions = _apply(
        issuer,
        person=person,
        role=global_role,
        assignment=service.CohortAssignment(cohort.id, track_id=track.id, course_ids=(b.id, a.id, b.id)),
        role_policy=policy,
    )
    session, command, authority, delegated_policy = calls[0]
    assert session is db
    assert delegated_policy is policy
    assert command.tenant_id == tenant_id
    assert command.person_id == person.id
    assert command.cohort_id == cohort.id
    assert command.track_id == track.id
    assert command.selected_course_ids == (b.id, a.id, b.id)
    assert command.global_role == global_role
    assert authority.tenant_id == tenant_id
    assert authority.actor_is_admin is actor_is_admin
    note = "" if previous is None else f" — reactivated (was {previous})"
    first = "Cohort cohort — already an instructor, role unchanged" if preserved else "Cohort cohort as student"
    assert descriptions == [
        first + note,
        "Track track in Cohort",
        "B course for Cohort",
        "A course for Cohort",
        "B course for Cohort",
    ]


@pytest.mark.parametrize("error_name", ["ParticipationBadRequest", "ParticipationNotFound", "ParticipationForbidden"])
def test_assignment_translates_domain_refusal_and_retains_cause(issuer, monkeypatch, error_name):
    from app.services import account_invitations as service
    from app.services.exceptions import BadRequestError, NotFoundError
    from app.services.invitation_policy import AcademyInvitationEnrollmentPolicy

    person = _person(issuer.db, tenant_id=issuer.tid, email="refusal@a.edu", first_name="Real", last_name="Target")
    cohort = _cohort(issuer.db, issuer.tid)
    error = getattr(service.learning, error_name)("original message")

    def owner(*args, **kwargs):
        raise error

    monkeypatch.setattr(service.learning, "ensure_invitation_enrollment", owner)
    expected = NotFoundError if error_name == "ParticipationNotFound" else BadRequestError
    with pytest.raises(expected, match="original message") as caught:
        _apply(
            issuer,
            person=person,
            role="opaque",
            assignment=service.CohortAssignment(cohort.id),
            role_policy=AcademyInvitationEnrollmentPolicy({}, "student", InvitationEnrollmentInitialState(status="active")),
        )
    assert caught.value.__cause__ is error


def test_invalid_policy_refuses_before_identity_role_or_account_writes(issuer, monkeypatch):
    from app.services import account_invitations as service
    from app.services import invitation_issuance as issuance

    cohort = _cohort(issuer.db, issuer.tid)

    def refuse(db):
        raise ValueError("invalid invitation configuration")

    def unexpected(*args, **kwargs):
        pytest.fail("identity or account mutation reached before policy validation")

    monkeypatch.setattr(service, "invitation_enrollment_policy", refuse)
    for name in ("create_identity", "synchronize_aliases", "ensure_role", "issue_token"):
        monkeypatch.setattr(issuance._Context, name, unexpected)
    with pytest.raises(ValueError, match="invalid invitation configuration"):
        _run(
            issuer,
            email="valid@example.com",
            first_name="F",
            last_name="L",
            role="student",
            assignments=(service.CohortAssignment(cohort.id),),
        )


def test_assignment_propagates_unexpected_owner_failure(issuer, monkeypatch):
    from app.services import account_invitations as service
    from app.services.invitation_policy import AcademyInvitationEnrollmentPolicy

    person = _person(issuer.db, tenant_id=issuer.tid, email="failure@a.edu", first_name="Real", last_name="Target")
    cohort = _cohort(issuer.db, issuer.tid, "C")
    failure = RuntimeError("transaction changed")

    def owner(*args, **kwargs):
        raise failure

    monkeypatch.setattr(service.learning, "ensure_invitation_enrollment", owner)
    with pytest.raises(RuntimeError) as caught:
        _apply(
            issuer,
            person=person,
            role="opaque",
            assignment=service.CohortAssignment(cohort.id),
            role_policy=AcademyInvitationEnrollmentPolicy({}, "student", InvitationEnrollmentInitialState(status="active")),
        )
    assert caught.value is failure


@pytest.mark.parametrize("foreign", [False, True])
def test_invite_resolves_one_policy_and_passes_it_to_each_assignment(issuer, monkeypatch, foreign):
    from uuid import uuid4

    from app.services import account_invitations as service
    from app.services import invitation_issuance as issuance
    from app.services.exceptions import BadRequestError
    from app.services.invitation_policy import AcademyInvitationEnrollmentPolicy

    tenant_id, db = issuer.tid, issuer.db
    person = _person(db, tenant_id=tenant_id, email="valid@example.com", first_name="F", last_name="L")
    _credential(
        db, tenant_id=tenant_id, person_id=person.id, email=person.email, password_hash=hash_password("existing9")
    )
    # Custom global-role issuance now has real admin cookie authority; the old
    # fake actor_is_admin=False was not proof of authority for assigning it.
    from dotmac_kernel.models import Role

    db.add(Role(tenant_id=tenant_id, slug="opaque", name="Opaque"))
    db.flush()
    policy = AcademyInvitationEnrollmentPolicy({}, "student", InvitationEnrollmentInitialState(status="active"))
    events = []

    def configured(session):
        assert session is db
        events.append("policy")
        return policy

    original_sync = issuance._Context.synchronize_aliases

    def sync(context):
        events.append("identity")
        return original_sync(context)

    def apply(context, **kwargs):
        assert context.db is db
        assert kwargs["role_policy"] is policy
        assert context.actor_is_admin is True
        assert kwargs["role"] == "opaque"
        cohort, _courses, _track = context.assignment_facts(kwargs["assignment"])
        events.append("assignment")
        return [cohort.name]

    monkeypatch.setattr(service, "invitation_enrollment_policy", configured)
    monkeypatch.setattr(issuance._Context, "synchronize_aliases", sync)
    monkeypatch.setattr(service, "_apply_assignment", apply)
    cohorts = [_cohort(db, tenant_id, name) for name in ("One", "Two")]
    assignments = tuple(service.CohortAssignment(uuid4() if foreign else cohort.id) for cohort in cohorts)
    kwargs = dict(email="valid@example.com", first_name="F", last_name="L", role="opaque", assignments=assignments)
    if foreign:
        with pytest.raises(BadRequestError, match="cohort does not belong to this tenant"):
            _run(issuer, **kwargs)
        assert events == ["policy", "identity"]
    else:
        result = _run(issuer, **kwargs)
        assert result.assignments == ["One", "Two"]
        assert result.token is None
        # Real flushes occur at multiple canonical persistence boundaries; the
        # old single fake-db flush trace is replaced by actual flushed results.
        assert events == ["policy", "identity", "assignment", "assignment"]
        assert db.scalars(select(UserCredential).where(UserCredential.party_id == person.id)).first() is not None


def test_account_only_invite_skips_unused_enrollment_policy(issuer, monkeypatch):
    from app.services import account_invitations as service
    from app.services import invitation_issuance as issuance

    tenant_id, db = issuer.tid, issuer.db
    person = _person(db, tenant_id=tenant_id, email="account@example.com", first_name="F", last_name="L")
    events = []

    def unused_policy(session):
        raise AssertionError("account-only invitation must not read enrollment policy")

    original_sync, original_role, original_issue = (
        issuance._Context.synchronize_aliases,
        issuance._Context.ensure_role,
        issuance._Context.issue_token,
    )

    def sync(context):
        assert context.db is db
        events.extend(["identity", "sync"])
        return original_sync(context)

    def grant(context, role):
        assert context.db is db
        assert context.tenant_id == tenant_id and context.target_id == person.id and role == "student"
        events.append("role")
        return original_role(context, role)

    def issue(context, **kwargs):
        assert context.db is db
        assert kwargs == {"ttl_hours": None}
        events.append("invite")
        return original_issue(context, **kwargs)

    def entropy(count):
        assert count == 32
        return "a" * 64

    monkeypatch.setattr(service, "invitation_enrollment_policy", unused_policy)
    monkeypatch.setattr(issuance._Context, "synchronize_aliases", sync)
    monkeypatch.setattr(issuance._Context, "ensure_role", grant)
    monkeypatch.setattr(issuance._Context, "issue_token", issue)
    monkeypatch.setattr(issuance.secrets, "token_hex", entropy)
    result = _run(issuer, email=" ACCOUNT@EXAMPLE.COM ", first_name="F", last_name="L", role="student", assignments=())
    assert result.person.id == person.id and result.person.email == person.email
    from dataclasses import FrozenInstanceError

    with pytest.raises(FrozenInstanceError):
        result.person.email = "mutable-authority"
    assert result.token == "a" * 64
    assert result.created_person is False
    assert result.had_credential is False
    assert result.assignments == []
    assert events == ["identity", "sync", "role", "invite"]



def test_configured_new_status_is_one_snapshot_for_every_assignment_and_repeat_activates(issuer, monkeypatch):
    from app.services import account_invitations as service
    from app.services import settings_store

    db = issuer.db
    cohorts = [_cohort(db, issuer.tid, name) for name in ("Policy One", "Policy Two")]
    monkeypatch.setattr(settings_store.settings, "invitation_enrollment_initial_status", "waitlisted")
    original_factory = service.invitation_enrollment_policy
    original_owner = service.learning.ensure_invitation_enrollment
    snapshots, delegated = [], []

    def configured(session):
        policy = original_factory(session)
        snapshots.append(policy)
        # A later Settings change must not split one operation's assignments.
        monkeypatch.setattr(settings_store.settings, "invitation_enrollment_initial_status", "active")
        return policy

    def owner(session, command, *, authority, role_policy, initial_state):
        delegated.append((role_policy, initial_state))
        return original_owner(session, command, authority=authority, role_policy=role_policy, initial_state=initial_state)

    monkeypatch.setattr(service, "invitation_enrollment_policy", configured)
    monkeypatch.setattr(service.learning, "ensure_invitation_enrollment", owner)
    kwargs = dict(email="policy-snapshot@example.com", first_name="Policy", last_name="Snapshot", role="student",
                  assignments=tuple(CohortAssignment(cohort.id) for cohort in cohorts))
    first = _run(issuer, **kwargs)
    rows = db.scalars(select(Enrollment).where(Enrollment.person_id == first.person.id)).all()
    assert len(rows) == 2
    assert {row.status for row in rows} == {"waitlisted"}
    assert {row.role_in_cohort for row in rows} == {"student"}
    assert len(snapshots) == 1
    assert len(delegated) == 2
    assert all(role_policy is snapshots[0] and state is snapshots[0].initial_state for role_policy, state in delegated)
    assert snapshots[0].initial_state.status == "waitlisted"
    second = _run(issuer, **kwargs)
    assert second.person.id == first.person.id
    assert len(snapshots) == 2
    assert len(delegated) == 4
    assert all(row.status == "active" for row in rows)
    assert all("reactivated (was waitlisted)" in description for description in second.assignments)
