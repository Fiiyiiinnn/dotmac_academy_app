"""Hosted canonical bearer canaries, authored NOT RUN; assembly gates apply."""

from copy import copy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from dotmac_kernel.audit import AuditEvent
from dotmac_kernel.models import Party, PartyRoleGrant, Role
from dotmac_kernel.session_runtime import resolve_database_runtime
from dotmac_learning.participation_models import Cohort, CohortTrack, Enrollment, Track
from sqlalchemy import select, text

from app.models.account_token import AccountToken
from app.models.admissions import Applicant
from app.models.email_outbox import EmailOutbox
from app.models.onboarding import OnboardingTask
from app.services import admissions, onboarding
from app.services import invitation_issuance as issuance
from app.services.exceptions import BadRequestError
from app.services.security import hash_password, hash_token
from legacy_probe.services.admission_request_fixtures import admission_admin_request
from legacy_probe.services.test_canonical_current_user import current_account
from legacy_probe.services.test_canonical_invitation_issuance import _credential, _person

assert current_account


@pytest.fixture
def onboarding_case(current_account):
    a = current_account
    raw = uuid4().hex + uuid4().hex
    cohort = Cohort(tenant_id=a.tid, name="Bearer cohort", discipline="fiber", status="active")
    track = Track(tenant_id=a.tid, name="Bearer track", slug=uuid4().hex, status="active")
    a.admin.add_all([cohort, track])
    a.admin.flush()
    a.admin.add(CohortTrack(tenant_id=a.tid, cohort_id=cohort.id, track_id=track.id, status="active"))
    a.admin.flush()
    row = Applicant(
        tenant_id=a.tid,
        email=f"onboarding-{uuid4().hex}@a.edu",
        first_name="On",
        last_name="Board",
        status="onboarding",
        cohort_id=cohort.id,
        track_id=track.id,
        onboarding_token_hash=hash_token(raw),
    )
    a.admin.add(row)
    a.admin.flush()
    # This fixture begins at genuine persisted onboarding state. Earlier
    # assessment/admin audit bridges remain owed in the complete regression.
    onboarding.seed_tasks(a.admin, tenant_id=a.tid, applicant_id=row.id)
    for task in onboarding.list_tasks(a.admin, tenant_id=a.tid, applicant_id=row.id):
        onboarding.set_task_status(a.admin, task_id=task.id, status="done")
    a.admin.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert a.db.scalar(text("SELECT current_user")) == "app_user"
    return SimpleNamespace(a=a, id=row.id, raw=raw, email=row.email, cohort_id=cohort.id, track_id=track.id)


def _finish(case, *, key="orientation", enqueue=None, raw=None, tenant_id=None):
    with issuance.finish_onboarding_step(
        case.a.db, tenant_id=tenant_id or case.a.tid, raw=case.raw if raw is None else raw, key=key
    ) as result:
        if enqueue:
            enqueue(result)
    return result


def _snapshot(case):
    db, tid = case.a.db, case.a.tid
    db.expire_all()
    return (
        [
            (r.id, r.status, r.person_id, r.onboarding_token_hash)
            for r in db.scalars(select(Applicant).where(Applicant.id == case.id))
        ],
        [
            (r.id, r.status, r.completed_at)
            for r in db.scalars(
                select(OnboardingTask).where(OnboardingTask.applicant_id == case.id).order_by(OnboardingTask.id)
            )
        ],
        [
            (r.id, r.email, r.display_name, r.is_active)
            for r in db.scalars(select(Party).where(Party.tenant_id == tid).order_by(Party.id))
        ],
        [
            (r.id, r.party_id, r.role_id)
            for r in db.scalars(
                select(PartyRoleGrant).where(PartyRoleGrant.tenant_id == tid).order_by(PartyRoleGrant.id)
            )
        ],
        [
            (r.id, r.person_id, r.track_id, r.status, r.role_in_cohort)
            for r in db.scalars(select(Enrollment).where(Enrollment.tenant_id == tid).order_by(Enrollment.id))
        ],
        [
            (r.id, r.person_id, r.used_at, r.expires_at, r.token_hash)
            for r in db.scalars(select(AccountToken).where(AccountToken.tenant_id == tid).order_by(AccountToken.id))
        ],
        [
            (r.id, r.kind, r.recipient, r.status)
            for r in db.scalars(select(EmailOutbox).where(EmailOutbox.tenant_id == tid).order_by(EmailOutbox.id))
        ],
        [
            (r.id, r.action, r.entity_id, r.details)
            for r in db.scalars(select(AuditEvent).where(AuditEvent.tenant_id == tid).order_by(AuditEvent.id))
        ],
    )


def test_genuine_bearer_enrollment_system_audits_and_repeat(onboarding_case):
    c = onboarding_case
    result = _finish(c, enqueue=lambda result: False)
    assert result.enrolled_now and result.invite_raw
    assert result.invite_raw not in repr(result)
    assert result.applicant.person_id is not None and result.applicant.status == "enrolled"
    tokens = c.a.db.scalars(select(AccountToken).where(AccountToken.person_id == result.applicant.person_id)).all()
    assert len(tokens) == 1 and tokens[0].token_hash == hash_token(result.invite_raw)
    audits = c.a.db.scalars(
        select(AuditEvent).where(AuditEvent.entity_id.in_([str(c.id), str(result.applicant.person_id)]))
    ).all()
    assert {r.action for r in audits} == {"applicant.transition", "account.invitation.completed"}
    assert all(
        (r.actor_type, r.actor_id, r.actor_party_id, r.source_application)
        == ("system", "onboarding", None, "dotmac_academy_app")
        for r in audits
    )
    assert all(c.raw not in repr(r.details) and result.invite_raw not in repr(r.details) for r in audits)
    previous = next(
        t.completed_at
        for t in onboarding.list_tasks(c.a.db, tenant_id=c.a.tid, applicant_id=c.id)
        if t.key == "orientation"
    )
    again = _finish(c)
    assert not again.enrolled_now and again.invite_raw is None
    assert (
        next(
            t.completed_at
            for t in onboarding.list_tasks(c.a.db, tenant_id=c.a.tid, applicant_id=c.id)
            if t.key == "orientation"
        )
        >= previous
    )
    assert (
        c.a.db.scalar(select(AccountToken.id).where(AccountToken.person_id == result.applicant.person_id))
        == tokens[0].id
    )


@pytest.mark.parametrize("mode", ["invalid", "rotated", "cross_tenant"])
def test_invalid_rotated_cross_tenant_have_zero_effects(onboarding_case, mode):
    c = onboarding_case
    before = _snapshot(c)
    raw, tenant = "invalid", c.a.tid
    if mode == "rotated":
        applicant = c.a.db.get(Applicant, c.id)
        admissions.mint_onboarding_token(c.a.db, applicant=applicant)
        before = _snapshot(c)
        raw = c.raw
    elif mode == "cross_tenant":
        raw, tenant = c.raw, uuid4()
    assert _finish(c, raw=raw, tenant_id=tenant) is None
    assert _snapshot(c) == before


@pytest.mark.parametrize("reason", ["enrolled", "rejected", "missing_cohort", "incomplete"])
def test_task_precedes_automatic_eligibility_without_identity_policy(onboarding_case, monkeypatch, reason):
    c = onboarding_case
    row = c.a.db.get(Applicant, c.id)
    if reason in {"enrolled", "rejected"}:
        row.status = reason
    elif reason == "missing_cohort":
        row.cohort_id = None
    else:
        task = next(
            t for t in onboarding.list_tasks(c.a.db, tenant_id=c.a.tid, applicant_id=c.id) if t.key == "confirm_details"
        )
        onboarding.set_task_status(c.a.db, task_id=task.id, status="pending")
    c.a.db.flush()

    def forbidden(*args, **kwargs):
        pytest.fail("task-only outcomes must not compose creation policy")

    monkeypatch.setattr(issuance, "initial_identity_policy", forbidden)
    monkeypatch.setattr(admissions, "_enroll_locked", forbidden)
    result = _finish(c)
    assert not result.enrolled_now and result.invite_raw is None
    assert (
        next(
            t.status
            for t in onboarding.list_tasks(c.a.db, tenant_id=c.a.tid, applicant_id=c.id)
            if t.key == "orientation"
        )
        == "done"
    )


@pytest.mark.parametrize("has_credential", [False, True])
def test_exact_reuse_preserves_inactive_identity_custom_grants_and_any_alias_suppresses_token(
    onboarding_case, has_credential
):
    c = onboarding_case
    person = _person(
        c.a.db,
        tenant_id=c.a.tid,
        email=c.email,
        first_name="Preserved",
        last_name="Name",
        status="suspended",
        is_active=False,
    )
    custom = Role(tenant_id=c.a.tid, slug=uuid4().hex, name="Keep")
    c.a.db.add(custom)
    c.a.db.flush()
    grant = PartyRoleGrant(tenant_id=c.a.tid, party_id=person.id, role_id=custom.id)
    c.a.db.add(grant)
    c.a.db.flush()
    if has_credential:
        for alias in ("other-one@a.edu", "other-two@a.edu"):
            _credential(
                c.a.db,
                tenant_id=c.a.tid,
                person_id=person.id,
                email=alias,
                password_hash=hash_password("chosen-password9"),
            )
    result = _finish(c)
    assert result.applicant.person_id == person.id
    assert bool(result.invite_raw) is not has_credential
    stored = c.a.db.get(Party, person.id)
    assert (stored.display_name, stored.is_active, stored.email) == ("Preserved Name", False, c.email)
    assert c.a.db.get(PartyRoleGrant, grant.id).role_id == custom.id


@pytest.mark.parametrize(
    "stage",
    [
        "create_person",
        "ensure_admission_enrollment",
        "transition_audit",
        "ensure_grant",
        "issue_token",
        "invitation_audit",
        "enqueue",
    ],
)
def test_each_stage_failure_caught_then_outer_commit_preserves_zero_effects(onboarding_case, monkeypatch, stage):
    c = onboarding_case
    from app.services import canonical_identity_store as store

    before = _snapshot(c)
    failure = RuntimeError("injected stage failure")
    if stage == "enqueue":

        def enqueue(result):
            from app.services import applicant_email

            applicant_email.send_enrollment_welcome(
                c.a.db,
                applicant=result.applicant,
                setup_url=f"https://academy.example/accept-invite?token={result.invite_raw}",
            )
            raise failure
    else:
        enqueue = None
        owner, name = {
            "create_person": (store, "create_person"),
            "ensure_admission_enrollment": (admissions.learning, "ensure_admission_enrollment"),
            "transition_audit": (issuance._OnboardingContext, "_audit"),
            "ensure_grant": (store, "ensure_grant"),
            "issue_token": (issuance, "_issue_invitation_token"),
            "invitation_audit": (issuance._OnboardingContext, "_audit"),
        }[stage]
        original = getattr(owner, name)

        def after(*args, **kwargs):
            result = original(*args, **kwargs)
            if name != "_audit" or (
                args[1] == ("applicant.transition" if stage == "transition_audit" else "account.invitation.completed")
            ):
                raise failure
            return result

        monkeypatch.setattr(owner, name, after)
    root = c.a.db.get_transaction()
    with pytest.raises(RuntimeError) as caught:
        _finish(c, enqueue=enqueue)
    assert caught.value is failure and c.a.db.get_transaction() is root and c.a.db.is_active
    c.a.db.commit()
    resolve_database_runtime().set_tenant(c.a.db, c.a.tid)
    assert _snapshot(c) == before
    assert c.a.db.scalar(text("SELECT current_setting('app.current_tenant')")) == str(c.a.tid)


def test_copied_and_cross_root_context_cannot_select_system_transition(onboarding_case):
    from dotmac_kernel.transactions import conflict_savepoint, conflict_savepoint_requires_rollback

    from app.models.identity_extensions import AcademyAccountProfile

    c = onboarding_case
    db = c.a.db
    person = _person(db, tenant_id=c.a.tid, email=c.email, first_name="Existing", last_name="Learner")
    context = None
    try:
        # A test-owned private lifecycle uses ACTUAL bearer proof and actual
        # savepoint/root objects. No evidence/instance marker is manufactured.
        # Public finish_onboarding_step still clears on every exit unchanged.
        with conflict_savepoint(db):
            context = issuance._OnboardingContext(db, tenant_id=c.a.tid, digest=hash_token(c.raw), key="orientation")
            assert context.prove()
            root_a = db.get_transaction()
            assert context.root is root_a
            with pytest.raises(issuance.InvitationIssuanceError):
                admissions._transition_onboarding_enrollment(copy(context))
        # Leave this test-owned context uncleared until the attack completes.
        db.commit()
        resolve_database_runtime().set_tenant(db, c.a.tid)
        root_b = db.get_transaction()
        assert root_a is not root_b and root_b is not None and root_b.is_active
        assert not root_a.is_active and context.root is root_a
        assert db.is_active and context.db is db and context.bound_session is db
        assert context._issued_context is context
        assert context.evidence is not None and context.evidence is context._issued_evidence
        assert context.digest == hash_token(c.raw) and context.tenant_id == c.a.tid
        assert not conflict_savepoint_requires_rollback(db, expected_root=root_a)
        assert db.scalar(text("SELECT current_setting('app.current_tenant')")) == str(c.a.tid)
        assert context.applicant.id == c.id and context.applicant.onboarding_token_hash == hash_token(c.raw)
        assert db.get(Party, person.id).tenant_id == c.a.tid
        assert db.get(AcademyAccountProfile, person.id).status == "active"
        context._check_bearer()  # bearer, exact resolution, link and intake still valid
        # With all other require() predicates valid, removing current-root
        # equality makes this require() negative canary fail to raise.
        with pytest.raises(issuance.InvitationIssuanceError, match="private context refused"):
            context.require()
        with pytest.raises(issuance.InvitationIssuanceError, match="private context refused"):
            admissions._transition_onboarding_enrollment(context)
        assert db.get_transaction() is root_b and db.is_active
    finally:
        if context is not None:
            context.clear()


def test_live_context_refuses_actual_foreign_session_and_root(onboarding_case):
    from dotmac_kernel.transactions import conflict_savepoint, conflict_savepoint_requires_rollback
    from sqlalchemy.orm import sessionmaker

    c = onboarding_case
    db = c.a.db
    SessionLocal = sessionmaker(bind=db.get_bind(), autoflush=False)
    context = None
    try:
        with conflict_savepoint(db):
            context = issuance._OnboardingContext(db, tenant_id=c.a.tid, digest=hash_token(c.raw), key="orientation")
            assert context.prove()
            root_a, evidence = db.get_transaction(), context.evidence
            with SessionLocal() as foreign:
                resolve_database_runtime().set_tenant(foreign, c.a.tid)
                root_b = foreign.get_transaction()
                assert foreign is not db and root_b is not root_a
                assert foreign.is_active and root_b is not None and root_b.is_active
                assert foreign.scalar(text("SELECT current_setting('app.current_tenant')")) == str(c.a.tid)
                assert not conflict_savepoint_requires_rollback(foreign, expected_root=root_a)
                # Attack the bound Session reference only; issued evidence,
                # original Session binding and root remain the genuine originals.
                context.db = foreign
                try:
                    assert context.bound_session is db and context.root is root_a
                    assert context.evidence is evidence and context._issued_evidence is evidence
                    assert context._issued_context is context
                    with pytest.raises(issuance.InvitationIssuanceError, match="private context refused"):
                        context.require()
                    with pytest.raises(issuance.InvitationIssuanceError, match="private context refused"):
                        admissions._transition_onboarding_enrollment(context)
                finally:
                    context.db = db
            assert context.root is root_a and db.get_transaction() is root_a
            context.require()
            context._check_bearer()
    finally:
        if context is not None:
            context.db = db
            context.clear()


@pytest.mark.parametrize("missing_key", ["orientation", "confirm_details"])
def test_missing_task_key_remains_noop_and_auto_gate_still_runs(onboarding_case, missing_key):
    c = onboarding_case
    task = next(t for t in onboarding.list_tasks(c.a.db, tenant_id=c.a.tid, applicant_id=c.id) if t.key == missing_key)
    c.a.db.delete(task)
    c.a.db.flush()
    result = _finish(c, key=missing_key)
    assert result.enrolled_now
    assert not any(t.key == missing_key for t in onboarding.list_tasks(c.a.db, tenant_id=c.a.tid, applicant_id=c.id))


def test_seed_catalog_is_additive_and_admin_task_status_remains_independent_after_enrollment(onboarding_case):
    c = onboarding_case
    result = _finish(c)
    before = [
        (t.id, t.key, t.label, t.order_index)
        for t in onboarding.list_tasks(c.a.db, tenant_id=c.a.tid, applicant_id=c.id)
    ]
    for task in onboarding.list_tasks(c.a.db, tenant_id=c.a.tid, applicant_id=c.id):
        assert onboarding.set_task_status(c.a.db, task_id=task.id, status="pending").completed_at is None
        assert onboarding.set_task_status(c.a.db, task_id=task.id, status="done").completed_at is not None
    onboarding.seed_tasks(c.a.db, tenant_id=c.a.tid, applicant_id=c.id)
    after = [
        (t.id, t.key, t.label, t.order_index)
        for t in onboarding.list_tasks(c.a.db, tenant_id=c.a.tid, applicant_id=c.id)
    ]
    assert before == after
    assert result.applicant.status == "enrolled"
    assert (
        len(c.a.db.scalars(select(AccountToken).where(AccountToken.person_id == result.applicant.person_id)).all()) == 1
    )


def test_configured_new_state_role_and_inflight_policy_are_pinned(onboarding_case, monkeypatch):
    import json

    from app.models.identity_extensions import AcademyAccountProfile
    from app.services import settings_store

    c = onboarding_case
    settings_store.set_many(
        c.a.admin,
        {
            "account_initial_is_active": "false",
            "account_initial_profile_status": "suspended",
            "admission_enrollment_initial_role": "instructor",
            "admission_enrollment_initial_status": "dropped",
            "onboarding_enrollment_global_role": "custom-onboarding",
            "web_account_creation_role_provisioning": json.dumps({"custom-onboarding": "Custom onboarding"}),
            "invitation_onboarding_replace_outstanding": "false",
        },
    )
    c.a.admin.commit()
    from app.services import admission_enrollment_policy as admission_policy

    factory = admission_policy.admission_enrollment_policy
    snapshots = []

    def composed(db):
        state = factory(db)
        snapshots.append(state)
        return state

    monkeypatch.setattr(admission_policy, "admission_enrollment_policy", composed)
    owner = admissions.learning.ensure_admission_enrollment
    pinned = []

    def changed(session, command, *, initial_state):
        assert (initial_state.role_in_cohort, initial_state.status) == ("instructor", "dropped")
        assert len(snapshots) == 1 and initial_state is snapshots[0]
        pinned.append(initial_state)
        outcome = owner(session, command, initial_state=initial_state)
        settings_store.set_many(
            c.a.admin,
            {
                "admission_enrollment_initial_role": "student",
                "admission_enrollment_initial_status": "active",
            },
        )
        c.a.admin.commit()
        return outcome

    monkeypatch.setattr(admissions.learning, "ensure_admission_enrollment", changed)
    issuance_factory = issuance.invitation_issuance_policy
    issuer_snapshots = []

    def issuance_composed(db, *, purpose):
        selected = issuance_factory(db, purpose=purpose)
        issuer_snapshots.append(selected)
        settings_store.set_many(
            c.a.admin,
            {
                "onboarding_enrollment_global_role": "different-role",
                "invitation_onboarding_replace_outstanding": "true",
            },
        )
        c.a.admin.commit()
        return selected

    monkeypatch.setattr(issuance, "invitation_issuance_policy", issuance_composed)
    result = _finish(c)
    assert len(issuer_snapshots) == 1 and issuer_snapshots[0].replace_outstanding is False
    party = c.a.db.get(Party, result.applicant.person_id)
    profile = c.a.db.get(AcademyAccountProfile, party.id)
    assert party.is_active is False and profile.status == "suspended"
    assert len(pinned) == len(snapshots) == 1
    row = c.a.db.scalars(select(Enrollment).where(Enrollment.person_id == party.id)).one()
    assert (row.role_in_cohort, row.status) == (pinned[0].role_in_cohort, pinned[0].status)
    slugs = c.a.db.scalars(
        select(Role.slug)
        .join(PartyRoleGrant, PartyRoleGrant.role_id == Role.id)
        .where(PartyRoleGrant.tenant_id == c.a.tid, PartyRoleGrant.party_id == party.id)
    ).all()
    assert slugs == ["custom-onboarding"]


@pytest.mark.parametrize(
    "caller,occurrence",
    [
        ("complete", 1),
        ("create_person", 1),
        ("create_person", 2),
        ("create_person", 3),
        ("_enroll_locked", 1),
        ("_transition_applicant_locked", 1),
        ("ensure_grant", 1),
        ("_issue_invitation_token", 1),
        ("_audit", 1),
        ("_audit", 2),
        ("enqueue_email", 1),
    ],
)
def test_failure_after_every_owned_flush_recovers_then_outer_commit(onboarding_case, monkeypatch, caller, occurrence):
    import inspect

    from app.services import applicant_email

    c = onboarding_case
    before = _snapshot(c)
    root = c.a.db.get_transaction()
    flush = c.a.db.flush
    seen = 0

    def failed_flush(*args, **kwargs):
        nonlocal seen
        outcome = flush(*args, **kwargs)
        if inspect.currentframe().f_back.f_code.co_name == caller:
            seen += 1
            if seen == occurrence:
                raise RuntimeError("owned flush failure")
        return outcome

    monkeypatch.setattr(c.a.db, "flush", failed_flush)

    def enqueue(result):
        applicant_email.send_enrollment_welcome(
            c.a.db,
            applicant=result.applicant,
            setup_url=f"https://academy.example/accept-invite?token={result.invite_raw}",
        )

    with pytest.raises(RuntimeError, match="owned flush failure"):
        _finish(c, enqueue=enqueue)
    assert seen >= occurrence and c.a.db.get_transaction() is root and c.a.db.is_active
    c.a.db.commit()
    resolve_database_runtime().set_tenant(c.a.db, c.a.tid)
    assert _snapshot(c) == before


def test_source_string_and_absent_actor_cannot_select_automatic_audit(onboarding_case, monkeypatch):
    from dotmac_kernel.transactions import conflict_savepoint

    c = onboarding_case
    calls = []

    def legacy(*args, **kwargs):
        calls.append(kwargs)
        raise RuntimeError("legacy mapping remains owed")

    def canonical(*args, **kwargs):
        pytest.fail("a public source string cannot authorize canonical attribution")

    monkeypatch.setattr(admissions, "_legacy_write_audit_event", legacy)
    monkeypatch.setattr(issuance, "write_audit_event", canonical)
    before = _snapshot(c)
    with pytest.raises(BadRequestError, match="genuine Request"):
        with conflict_savepoint(c.a.db):
            admissions.transition_applicant(
                c.a.db, applicant_id=c.id, to_status="enrolled", actor_person_id=None, source="onboarding"
            )
    assert len(calls) == 0
    assert _snapshot(c) == before
    assert c.a.db.get(Applicant, c.id).status == "onboarding"


def test_exact_email_miss_does_not_silently_become_normalized_reuse(onboarding_case):
    from sqlalchemy.exc import IntegrityError

    c = onboarding_case
    existing = _person(c.a.db, tenant_id=c.a.tid, email=c.email.upper(), first_name="Keep", last_name="Case")
    before = _snapshot(c)
    root = c.a.db.get_transaction()
    with pytest.raises(IntegrityError):
        _finish(c)
    assert c.a.db.get_transaction() is root and c.a.db.is_active
    c.a.db.commit()
    resolve_database_runtime().set_tenant(c.a.db, c.a.tid)
    assert _snapshot(c) == before
    assert c.a.db.get(Party, existing.id).email == c.email.upper()


def test_catalog_changes_apply_only_to_new_rows_and_ordered_admin_list(onboarding_case):
    import json

    from app.services import settings_store

    c = onboarding_case
    before = [
        (t.id, t.key, t.label, t.order_index, t.status)
        for t in onboarding.list_tasks(c.a.db, tenant_id=c.a.tid, applicant_id=c.id)
    ]
    catalog = [
        ("orientation", "First custom label"),
        ("confirm_details", "Second custom label"),
        ("entrance_assessment", "Third custom label"),
    ]
    settings_store.set_many(c.a.admin, {"onboarding_task_catalog": json.dumps(catalog)})
    c.a.admin.commit()
    onboarding.seed_tasks(c.a.db, tenant_id=c.a.tid, applicant_id=c.id)
    assert [
        (t.id, t.key, t.label, t.order_index, t.status)
        for t in onboarding.list_tasks(c.a.db, tenant_id=c.a.tid, applicant_id=c.id)
    ] == before
    new = Applicant(
        tenant_id=c.a.tid,
        email=f"new-catalog-{uuid4().hex}@a.edu",
        first_name="New",
        last_name="Applicant",
        status="onboarding",
        cohort_id=c.cohort_id,
        track_id=c.track_id,
    )
    c.a.db.add(new)
    c.a.db.flush()
    tasks = onboarding.seed_tasks(c.a.db, tenant_id=c.a.tid, applicant_id=new.id)
    assert [(t.key, t.label) for t in tasks] == catalog
    assert [t.order_index for t in tasks] == [0, 1, 2]
    assert all(t.status == "pending" and t.completed_at is None for t in tasks)


@pytest.mark.parametrize("role,status", [("student", "active"), ("instructor", "dropped")])
@pytest.mark.parametrize("entry", ["bearer", "explicit"])
def test_actual_admission_state_and_public_reader_interpretation(onboarding_case, role, status, entry):
    from dotmac_learning.models import Course
    from dotmac_learning.participation_models import CourseOffering, TrackCourse

    from app.services import settings_store

    c = onboarding_case
    settings_store.set_many(
        c.a.admin, {"admission_enrollment_initial_role": role, "admission_enrollment_initial_status": status}
    )
    course = Course(
        tenant_id=c.a.tid,
        slug=uuid4().hex,
        title="State reader",
        discipline="fiber",
        source_ref="admission-state-test",
        version=1,
        status="published",
        listed=False,
    )
    c.a.admin.add(course)
    c.a.admin.flush()
    c.a.admin.add_all(
        [
            CourseOffering(tenant_id=c.a.tid, cohort_id=c.cohort_id, course_id=course.id, status="active"),
            TrackCourse(tenant_id=c.a.tid, track_id=c.track_id, course_id=course.id, order_index=0),
        ]
    )
    c.a.admin.commit()
    result = (
        _finish(c).applicant
        if entry == "bearer"
        else admissions.enroll_api_applicant(
            c.a.db, request=admission_admin_request(c.a.db, c.a.tenant), applicant_id=c.id, cohort_id=c.cohort_id
        )
    )
    enrollment = c.a.db.scalars(select(Enrollment).where(Enrollment.person_id == result.person_id)).one()
    assert (enrollment.role_in_cohort, enrollment.status, enrollment.audience) == (role, status, None)
    assert admissions.learning.account_cohort_names(c.a.db, tenant_id=c.a.tid, person_id=result.person_id) == [
        "Bearer cohort"
    ]
    visible = admissions.learning.visible_course_ids(c.a.db, tenant_id=c.a.tid, person_id=result.person_id)
    assert (course.id in visible) is (status == "active")
    slugs = c.a.db.scalars(
        select(Role.slug)
        .join(PartyRoleGrant, PartyRoleGrant.role_id == Role.id)
        .where(PartyRoleGrant.party_id == result.person_id, PartyRoleGrant.tenant_id == c.a.tid)
    ).all()
    assert slugs == (["student"] if entry == "bearer" else [])


@pytest.mark.parametrize("existing", [False, True])
def test_policy_refusal_after_task_write_caught_outer_commit_has_zero_effects(onboarding_case, existing):
    from app.models.platform_settings import PlatformSetting

    c = onboarding_case
    if existing:
        party = _person(c.a.admin, tenant_id=c.a.tid, email=c.email, first_name="Existing", last_name="Person")
        c.a.admin.add(
            Enrollment(
                tenant_id=c.a.tid,
                cohort_id=c.cohort_id,
                person_id=party.id,
                track_id=None,
                role_in_cohort="instructor",
                status="inactive",
                audience="staff",
                employee_ref="Keep",
            )
        )
    task = c.a.admin.scalars(
        select(OnboardingTask).where(OnboardingTask.applicant_id == c.id, OnboardingTask.key == "orientation")
    ).one()
    task.status, task.completed_at = "pending", None
    c.a.admin.add(PlatformSetting(key="admission_enrollment_initial_status", value=None))
    c.a.admin.commit()
    before = _snapshot(c)
    root = c.a.db.get_transaction()
    with pytest.raises(ValueError):
        _finish(c)
    assert c.a.db.get_transaction() is root and c.a.db.is_active
    c.a.db.commit()
    resolve_database_runtime().set_tenant(c.a.db, c.a.tid)
    assert _snapshot(c) == before


@pytest.mark.parametrize("role", ["student", "instructor"])
@pytest.mark.parametrize("status", ["active", "waitlisted", "dropped", "inactive"])
def test_valid_policy_preserves_existing_admission_state(onboarding_case, role, status):
    from datetime import UTC, datetime, timedelta

    from app.services import settings_store

    c = onboarding_case
    party = _person(c.a.admin, tenant_id=c.a.tid, email=c.email, first_name="Existing", last_name="Person")
    expiry = datetime.now(UTC) + timedelta(days=3)
    row = Enrollment(
        tenant_id=c.a.tid,
        cohort_id=c.cohort_id,
        person_id=party.id,
        track_id=None,
        role_in_cohort=role,
        status=status,
        audience="staff",
        employee_ref="Preserve",
        access_ends_at=expiry,
    )
    c.a.admin.add(row)
    settings_store.set_many(
        c.a.admin,
        {
            "admission_enrollment_initial_role": "configured-other",
            "admission_enrollment_initial_status": "other",
        },
    )
    c.a.admin.commit()
    result = _finish(c)
    actual = c.a.db.get(Enrollment, row.id)
    assert result.applicant.person_id == party.id
    assert actual.track_id == c.track_id
    assert (actual.role_in_cohort, actual.status, actual.audience, actual.employee_ref, actual.access_ends_at) == (
        role,
        status,
        "staff",
        "Preserve",
        expiry,
    )


@pytest.mark.parametrize("invalid_guard", ["intake", "target"])
def test_automatic_existing_guard_precedes_malformed_admission_policy(onboarding_case, invalid_guard):
    from sqlalchemy import delete, update

    from app.models.identity_extensions import AcademyAccountProfile
    from app.models.platform_settings import PlatformSetting
    from app.services.exceptions import BadRequestError

    c = onboarding_case
    if invalid_guard == "intake":
        c.a.admin.execute(update(Track).where(Track.id == c.track_id).values(status="inactive"))
        expected = "no active canonical training track"
    else:
        party = _person(c.a.admin, tenant_id=c.a.tid, email=c.email, first_name="Existing", last_name="Person")
        c.a.admin.execute(delete(AcademyAccountProfile).where(AcademyAccountProfile.party_id == party.id))
        expected = "Admission identity is incomplete"
    task = c.a.admin.scalars(
        select(OnboardingTask).where(OnboardingTask.applicant_id == c.id, OnboardingTask.key == "orientation")
    ).one()
    task.status, task.completed_at = "pending", None
    c.a.admin.add(PlatformSetting(key="admission_enrollment_initial_status", value=None))
    c.a.admin.commit()
    before = _snapshot(c)
    root = c.a.db.get_transaction()
    with pytest.raises(BadRequestError, match=expected):
        _finish(c)
    assert c.a.db.get_transaction() is root and c.a.db.is_active
    c.a.db.commit()
    resolve_database_runtime().set_tenant(c.a.db, c.a.tid)
    assert _snapshot(c) == before


def test_automatic_valid_guards_malformed_admission_policy_precedes_identity_and_enrollment_dml(
    onboarding_case, monkeypatch
):
    from app.models.platform_settings import PlatformSetting
    from app.services import canonical_identity_store as store

    c = onboarding_case
    c.a.admin.add(PlatformSetting(key="admission_enrollment_initial_role", value=None))
    c.a.admin.commit()
    before = _snapshot(c)
    root = c.a.db.get_transaction()

    def forbidden(*args, **kwargs):
        pytest.fail("malformed initial state must refuse before identity or enrollment DML")

    monkeypatch.setattr(store, "create_person", forbidden)
    monkeypatch.setattr(admissions.learning, "ensure_admission_enrollment", forbidden)
    with pytest.raises(ValueError, match="admission_enrollment_initial_role"):
        _finish(c)
    assert c.a.db.get_transaction() is root and c.a.db.is_active
    c.a.db.commit()
    resolve_database_runtime().set_tenant(c.a.db, c.a.tid)
    assert _snapshot(c) == before


@pytest.mark.parametrize("key", ["onboarding_enrollment_global_role", "invitation_token_hours"])
def test_later_automatic_policy_refusal_rolls_back_owned_effects_after_actual_admission(
    onboarding_case, monkeypatch, key
):
    from app.models.platform_settings import PlatformSetting
    from app.services import settings_store

    c = onboarding_case
    settings_store.set_many(c.a.admin, {key: "student" if key == "onboarding_enrollment_global_role" else "168"})
    task = c.a.admin.scalars(
        select(OnboardingTask).where(OnboardingTask.applicant_id == c.id, OnboardingTask.key == "orientation")
    ).one()
    task.status, task.completed_at = "pending", None
    c.a.admin.commit()
    before = _snapshot(c)
    root = c.a.db.get_transaction()
    enroll = admissions._enroll_locked
    calls = []

    def enrolled_then_malformed(db, **kwargs):
        applicant = enroll(db, **kwargs)
        assert applicant.status == "enrolled" and applicant.person_id is not None
        assert db.get(Party, applicant.person_id) is not None
        assert db.scalars(select(Enrollment).where(Enrollment.person_id == applicant.person_id)).one() is not None
        assert (
            db.scalars(
                select(AuditEvent).where(
                    AuditEvent.tenant_id == c.a.tid,
                    AuditEvent.action == "applicant.transition",
                    AuditEvent.entity_id == str(c.id),
                )
            ).first()
            is not None
        )
        calls.append(applicant.person_id)
        stored = c.a.admin.get(PlatformSetting, key)
        # A stored None falls back to the default, so plant a role that fails the strip check.
        stored.value = " student " if key == "onboarding_enrollment_global_role" else None
        c.a.admin.commit()
        return applicant

    monkeypatch.setattr(admissions, "_enroll_locked", enrolled_then_malformed)
    with pytest.raises(ValueError, match=key):
        _finish(c)
    assert len(calls) == 1
    assert c.a.db.get_transaction() is root and c.a.db.is_active
    c.a.db.commit()
    resolve_database_runtime().set_tenant(c.a.db, c.a.tid)
    assert _snapshot(c) == before
