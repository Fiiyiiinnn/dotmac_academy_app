"""Hosted human-issuer canaries, authored NOT RUN; no legacy identity shim.

Requires the real canonical assembly, Learning floor, token a003 adoption,
product audit enrollment and complete app_user fixture. Setup may use the
migration fixture for actor grants; every invitation operation uses app_user.
"""

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from dotmac_kernel.audit import AuditEvent
from dotmac_kernel.models import AuthSession, Party, PartyPerson, PartyRoleGrant, PartyType, Role, UserCredential
from dotmac_kernel.session_runtime import resolve_database_runtime
from dotmac_learning.models import Course
from dotmac_learning.participation_models import Cohort, CohortTrack, CourseOffering, Enrollment, Track
from sqlalchemy import delete, select, text

from app.models.account_token import AccountToken
from app.models.email_outbox import EmailOutbox
from app.models.identity_extensions import AcademyAccountProfile, AcademyCredentialPolicy
from app.services import account_invitations as decisions
from app.services import invitation_issuance as issuance
from app.services import settings_store
from app.services.invitation_policy import invitation_acceptance_policy
from app.services.security import hash_password, hash_token
from legacy_probe.services.test_canonical_current_user import _request, current_account

assert current_account


@pytest.fixture
def issuer(current_account):
    a = current_account
    _set_actor_role(a, "admin")
    assert a.db.scalar(text("SELECT current_user")) == "app_user"
    return a


def _set_actor_role(a, slug):
    a.admin.execute(delete(PartyRoleGrant).where(PartyRoleGrant.tenant_id == a.tid, PartyRoleGrant.party_id == a.pid))
    role = a.admin.scalars(select(Role).where(Role.tenant_id == a.tid, Role.slug == slug)).first()
    if role is None:
        role = Role(tenant_id=a.tid, slug=slug, name=slug.title())
        a.admin.add(role)
        a.admin.flush()
    a.admin.add(PartyRoleGrant(tenant_id=a.tid, party_id=a.pid, role_id=role.id))
    a.admin.commit()


def _person(db, *, tenant_id, email, first_name, last_name, status="active", is_active=True):
    now = datetime.now(UTC)
    party = Party(
        tenant_id=tenant_id,
        party_type=PartyType.person,
        email=email,
        display_name=f"{first_name} {last_name}",
        is_active=is_active,
    )
    db.add(party)
    db.flush()
    db.add(PartyPerson(party_id=party.id, first_name=first_name, last_name=last_name))
    db.flush()
    db.add(
        AcademyAccountProfile(
            tenant_id=tenant_id, party_id=party.id, status=status, prefs={}, created_at=now, updated_at=now
        )
    )
    db.flush()
    return decisions.InvitationAccountView(party.id, tenant_id, email, first_name, last_name, status, is_active)


def _credential(db, *, tenant_id, person_id, email, password_hash):
    now = datetime.now(UTC)
    row = UserCredential(tenant_id=tenant_id, party_id=person_id, password_hash=password_hash)
    db.add(row)
    db.flush()
    db.add(
        AcademyCredentialPolicy(
            tenant_id=tenant_id,
            credential_id=row.id,
            email=email,
            failed_login_attempts=3,
            locked_until=now + timedelta(hours=1),
            lifecycle_version=4,
            reset_required=True,
            created_at=now,
            updated_at=now,
        )
    )
    db.flush()
    return row


def _cohort(db, tenant_id, name="Invitation Cohort"):
    row = Cohort(tenant_id=tenant_id, name=name, discipline="networking", status="active")
    db.add(row)
    db.flush()
    return row


def _curriculum(db, tenant_id, cohort):
    track = Track(tenant_id=tenant_id, name="Track", slug=uuid4().hex, status="active")
    courses = [
        Course(tenant_id=tenant_id, title=title, slug=uuid4().hex, discipline="networking", source_ref="test", status="published", listed=False)
        for title in ("A", "B")
    ]
    db.add_all([track, *courses])
    db.flush()
    db.add(CohortTrack(tenant_id=tenant_id, cohort_id=cohort.id, track_id=track.id, status="active"))
    db.flush()
    return track, *courses


def _run(a, *, email, first_name, last_name, role, assignments=(), ttl_hours=None):
    from app.services.roles import role_slugs

    request = _request(a.tenant, a.token)
    if "admin" in role_slugs(a.db, a.tid, a.pid):
        operation = decisions.invite_and_enroll(
            a.db,
            request=request,
            email=email,
            first_name=first_name,
            last_name=last_name,
            role=role,
            assignments=assignments,
            ttl_hours=ttl_hours,
        )
    else:
        assert len(assignments) == 1 and role == "student"
        assignment = assignments[0]
        operation = issuance.invite_web_cohort_account(
            a.db,
            request=request,
            email=email,
            first_name=first_name,
            last_name=last_name,
            cohort_id=assignment.cohort_id,
            track_id=assignment.track_id,
            ttl_hours=ttl_hours,
        )
    with operation as result:
        pass
    return result


def _private_context(a, person, *, role="student", assignments=()):
    from app.services.roles import role_slugs

    purpose = "generic" if "admin" in role_slugs(a.db, a.tid, a.pid) else "cohort"
    command = issuance._Command(
        purpose, person.email, person.first_name, person.last_name, role, tuple(assignments), None, None
    )
    context = issuance._Context(
        a.db, tenant_id=a.tid, digest=hash_token(a.token), session_id=a.session_id, actor_id=a.pid, command=command
    )
    context.prove()
    return context


def _apply(a, *, person, role, assignment, role_policy):
    from dotmac_kernel.transactions import conflict_savepoint

    context = None
    try:
        with conflict_savepoint(a.db):
            context = _private_context(a, person, role=role, assignments=(assignment,))
            return decisions._apply_assignment(
                context, role=role, assignment=assignment, role_policy=role_policy, pacing_default_open=True
            )
    finally:
        if context is not None:
            context.clear()


def _state(a):
    a.db.expire_all()
    return (
        [
            (row.id, row.email, row.display_name, row.is_active)
            for row in a.db.scalars(select(Party).where(Party.tenant_id == a.tid).order_by(Party.id)).all()
        ],
        [
            (row.party_id, row.first_name, row.last_name)
            for row in a.db.scalars(
                select(PartyPerson)
                .join(Party, Party.id == PartyPerson.party_id)
                .where(Party.tenant_id == a.tid)
                .order_by(PartyPerson.party_id)
            ).all()
        ],
        [
            (row.party_id, row.status, row.prefs, row.updated_at)
            for row in a.db.scalars(
                select(AcademyAccountProfile)
                .where(AcademyAccountProfile.tenant_id == a.tid)
                .order_by(AcademyAccountProfile.party_id)
            ).all()
        ],
        [
            (row.id, row.party_id, row.role_id)
            for row in a.db.scalars(
                select(PartyRoleGrant).where(PartyRoleGrant.tenant_id == a.tid).order_by(PartyRoleGrant.id)
            ).all()
        ],
        [
            (row.id, row.party_id, row.password_hash)
            for row in a.db.scalars(
                select(UserCredential).where(UserCredential.tenant_id == a.tid).order_by(UserCredential.id)
            ).all()
        ],
        [
            (row.id, row.person_id, row.kind, row.token_hash, row.used_at, row.expires_at)
            for row in a.db.scalars(
                select(AccountToken).where(AccountToken.tenant_id == a.tid).order_by(AccountToken.id)
            ).all()
        ],
        [
            (row.id, row.person_id, row.role_in_cohort, row.status)
            for row in a.db.scalars(
                select(Enrollment).where(Enrollment.tenant_id == a.tid).order_by(Enrollment.id)
            ).all()
        ],
        [
            (row.id, row.slug, row.name)
            for row in a.db.scalars(select(Role).where(Role.tenant_id == a.tid).order_by(Role.id)).all()
        ],
        [
            (
                row.credential_id,
                row.email,
                row.failed_login_attempts,
                row.locked_until,
                row.lifecycle_version,
                row.reset_required,
                row.updated_at,
            )
            for row in a.db.scalars(
                select(AcademyCredentialPolicy)
                .where(AcademyCredentialPolicy.tenant_id == a.tid)
                .order_by(AcademyCredentialPolicy.credential_id)
            ).all()
        ],
        [
            (row.id, row.expires_at, row.revoked_at)
            for row in a.db.scalars(
                select(AuthSession).where(AuthSession.tenant_id == a.tid).order_by(AuthSession.id)
            ).all()
        ],
        [
            (row.id, row.cohort_id, row.course_id, row.status)
            for row in a.db.scalars(
                select(CourseOffering).where(CourseOffering.tenant_id == a.tid).order_by(CourseOffering.id)
            ).all()
        ],
        [
            (row.id, row.idempotency_key, row.kind, row.recipient, row.status)
            for row in a.db.scalars(
                select(EmailOutbox).where(EmailOutbox.tenant_id == a.tid).order_by(EmailOutbox.id)
            ).all()
        ],
        [
            row.id
            for row in a.db.scalars(
                select(AuditEvent).where(AuditEvent.tenant_id == a.tid).order_by(AuditEvent.id)
            ).all()
        ],
    )


def test_inactive_suspended_target_is_invited_without_implicit_activation(issuer):
    a = issuer
    target = _person(
        a.db,
        tenant_id=a.tid,
        email="inactive@a.edu",
        first_name=" Old ",
        last_name="Name",
        status="suspended",
        is_active=False,
    )
    result = _run(a, email=target.email, first_name="Ignored", last_name="Ignored", role="student")
    assert result.person.id == target.id and result.person.first_name == " Old "
    assert result.person.status == "suspended" and result.person.is_active is False
    assert result.token is not None and result.token not in repr(result)
    with pytest.raises(FrozenInstanceError):
        result.person.email = "forged"


def test_existing_custom_role_and_additive_standard_roles_preserve_names(issuer):
    a = issuer
    custom = Role(tenant_id=a.tid, slug="custom-training", name="Exact custom name")
    a.db.add(custom)
    a.db.flush()
    result = _run(a, email="custom@a.edu", first_name="Custom", last_name="Role", role=custom.slug)
    assert result.token is not None
    assert a.db.get(Role, custom.id).name == "Exact custom name"
    assert {row.slug for row in a.db.scalars(select(Role).where(Role.tenant_id == a.tid)).all()} >= {
        "student",
        "instructor",
        "admin",
        custom.slug,
    }


def test_ui_standard_role_closure_differs_from_strict_generic_custom_support(issuer):
    from app.services.exceptions import BadRequestError

    a = issuer
    before = _state(a)
    with pytest.raises(BadRequestError, match="Invalid role"):
        with issuance.invite_web_account(
            a.db, request=_request(a.tenant, a.token), email="x@a.edu", first_name="X", last_name="Y", role="custom"
        ):
            pass
    assert _state(a) == before


def test_resend_credential_present_and_strict_conflict_have_zero_mutations(issuer):
    from app.services.exceptions import ConflictError

    a = issuer
    target = _person(a.db, tenant_id=a.tid, email="member@a.edu", first_name="Member", last_name="Existing")
    _credential(
        a.db, tenant_id=a.tid, person_id=target.id, email=target.email, password_hash=hash_password("member-password9")
    )
    before = _state(a)
    with issuance.resend_web_account_invitation(
        a.db, request=_request(a.tenant, a.token), person_id=target.id
    ) as result:
        assert result.had_credential and result.token is None
    assert _state(a) == before
    with pytest.raises(ConflictError):
        with issuance.invite_user(
            a.db,
            request=_request(a.tenant, a.token),
            email=target.email,
            first_name="Ignored",
            last_name="Ignored",
            role="instructor",
        ):
            pass
    root = a.db.get_transaction()
    a.db.commit()

    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert root is not None and _state(a) == before


@pytest.mark.parametrize("hours", [0, -1, 0.25])
def test_explicit_native_ttl_and_human_replacement(issuer, hours):
    a = issuer
    first = _run(a, email="ttl@a.edu", first_name="T", last_name="L", role="student")
    before = datetime.now(UTC)
    second = _run(a, email="ttl@a.edu", first_name="Ignored", last_name="Ignored", role="student", ttl_hours=hours)
    after = datetime.now(UTC)
    rows = a.db.scalars(
        select(AccountToken).where(AccountToken.person_id == first.person.id).order_by(AccountToken.created_at)
    ).all()
    assert len(rows) == 2 and rows[0].used_at is not None and rows[1].used_at is None
    assert before + timedelta(hours=hours) <= rows[1].expires_at <= after + timedelta(hours=hours)
    assert rows[1].token_hash == hash_token(second.token)


def test_token_can_be_consumed_by_real_canonical_claim_owner_without_issuer_hashing(issuer, monkeypatch):
    from app.services.invitation_claim import accept_invite

    a = issuer

    def forbidden(*args, **kwargs):
        raise AssertionError("issuance reached credential hash owner")

    import dotmac_kernel.credential_lifecycle as owner

    original = owner.hash_password
    monkeypatch.setattr(owner, "hash_password", forbidden)
    result = _run(a, email="claim@a.edu", first_name="Claim", last_name="Invite", role="student")
    assert not a.db.scalars(select(UserCredential).where(UserCredential.party_id == result.person.id)).all()
    monkeypatch.setattr(owner, "hash_password", original)
    party = accept_invite(
        a.db,
        tenant_id=a.tid,
        raw=result.token,
        password="chosen-claim9",
        password_policy=invitation_acceptance_policy(a.db),
    )
    assert party.id == result.person.id
    assert a.db.scalars(select(UserCredential).where(UserCredential.party_id == party.id)).first() is not None


@pytest.mark.parametrize("stage", ["learning", "audit", "enqueue"])
def test_learning_audit_enqueue_failure_caught_outer_commit_restores_all_families(issuer, monkeypatch, stage):

    a = issuer
    cohort = _cohort(a.db, a.tid)
    _track, course, _other_course = _curriculum(a.db, a.tid, cohort)
    before = _state(a)
    root = a.db.get_transaction()
    if stage == "learning":
        original = decisions.learning.ensure_invitation_enrollment

        def fail(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("injected Learning failure")

        monkeypatch.setattr(decisions.learning, "ensure_invitation_enrollment", fail)
    elif stage == "audit":
        original = issuance.write_audit_event

        def fail(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("injected audit failure")

        monkeypatch.setattr(issuance, "write_audit_event", fail)
    with pytest.raises(issuance.InvitationIssuanceError):
        with issuance.invite_web_account(
            a.db,
            request=_request(a.tenant, a.token),
            email="rollback@a.edu",
            first_name="Roll",
            last_name="Back",
            role="student",
            assignments=(decisions.CohortAssignment(cohort.id, course_ids=(course.id,)),),
        ) as result:
            from app.services.email_outbox import enqueue_email

            assert enqueue_email(
                a.db,
                tenant_id=a.tid,
                idempotency_key=f"account-invite:{result.person.id}:{hash_token(result.token)}",
                kind="account_invite",
                recipient=result.person.email,
                subject="Invitation",
                html_body=f"<p>Activate: https://alpha.localhost/accept?token={result.token}</p>",
                text_body=f"https://alpha.localhost/accept?token={result.token}",
            )
            if stage == "enqueue":
                raise RuntimeError("injected enqueue failure")
    assert a.db.get_transaction() is root and a.db.is_active
    a.db.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert _state(a) == before


def test_durable_enqueue_idempotency_false_preserves_completed_invitation(issuer):
    from app.services.email_outbox import enqueue_email

    a = issuer
    with issuance.invite_web_account(
        a.db, request=_request(a.tenant, a.token), email="dedup@a.edu", first_name="D", last_name="D", role="student"
    ) as result:
        arguments = dict(
            tenant_id=a.tid,
            idempotency_key=f"account-invite:{result.person.id}:{hash_token(result.token)}",
            kind="account_invite",
            recipient=result.person.email,
            subject="Invitation",
            html_body=f"<p>{result.token}</p>",
        )
        assert enqueue_email(a.db, **arguments) is True
        assert enqueue_email(a.db, **arguments) is False
    rows = a.db.scalars(
        select(EmailOutbox).where(
            EmailOutbox.tenant_id == a.tid, EmailOutbox.idempotency_key == arguments["idempotency_key"]
        )
    ).all()
    assert len(rows) == 1
    assert (
        a.db.scalars(
            select(AccountToken).where(AccountToken.person_id == result.person.id, AccountToken.used_at.is_(None))
        ).first()
        is not None
    )
    event = a.db.scalars(
        select(AuditEvent).where(
            AuditEvent.tenant_id == a.tid,
            AuditEvent.entity_id == str(result.person.id),
            AuditEvent.action == "account.invitation.completed",
        )
    ).one()
    assert result.token not in repr(event.details)


def test_self_target_deduplicates_principal_without_changing_names_status_or_credentials(issuer):
    a = issuer
    before = a.db.get(Party, a.pid).display_name
    credential = a.db.scalars(select(UserCredential).where(UserCredential.party_id == a.pid)).one()
    old_hash = credential.password_hash
    result = _run(a, email=a.email, first_name="Ignored", last_name="Ignored", role="student")
    assert result.person.id == a.pid and result.token is None and result.had_credential
    assert a.db.get(Party, a.pid).display_name == before
    assert credential.password_hash == old_hash


def test_explicit_human_retention_policy_does_not_silently_use_replacement_default(issuer, monkeypatch):
    a = issuer
    monkeypatch.setattr(
        settings_store,
        "get_all",
        lambda db: {"invitation_human_replace_outstanding": "false", "invitation_token_hours": "12"},
    )
    first = _run(a, email="retention@a.edu", first_name="Retain", last_name="Invite", role="student")
    _run(a, email="retention@a.edu", first_name="Ignored", last_name="Ignored", role="student")
    rows = a.db.scalars(select(AccountToken).where(AccountToken.person_id == first.person.id)).all()
    assert len(rows) == 2 and all(row.used_at is None for row in rows)


@pytest.mark.parametrize("change", ["missing", "tampered", "wrong_tenant"])
def test_actual_cookie_and_request_tenant_refuse_without_any_mutations(issuer, tenant_b, change):
    from fastapi import HTTPException

    a = issuer
    before = _state(a)
    request = _request(
        tenant_b if change == "wrong_tenant" else a.tenant,
        None if change == "missing" else a.token + "changed" if change == "tampered" else a.token,
    )
    with pytest.raises(HTTPException) as caught:
        with issuance.invite_web_account(
            a.db, request=request, email="forbidden@a.edu", first_name="F", last_name="F", role="student"
        ):
            pass
    assert caught.value.status_code == 303 and caught.value.headers == {"Location": "/login"}
    assert _state(a) == before


@pytest.mark.parametrize("attack", ["evidence", "command", "root"])
def test_private_exact_context_cannot_be_copied_or_reused_across_root(issuer, attack):
    from dataclasses import replace


    a = issuer
    target = _person(a.db, tenant_id=a.tid, email="context@a.edu", first_name="Context", last_name="Target")
    context = _private_context(a, target)
    try:
        if attack == "evidence":
            context.evidence = object()
        elif attack == "command":
            context.command = replace(context.command)
        else:
            a.db.commit()
            resolve_database_runtime().set_tenant(a.db, a.tid)
        with pytest.raises(issuance.InvitationIssuanceError):
            context.require()
    finally:
        context.clear()
    assert context.db is None and context.digest is None and context.token_digest is None


def test_credential_membership_drift_before_token_issue_is_refused_and_rolled_back(issuer, monkeypatch):

    a = issuer
    target = _person(a.db, tenant_id=a.tid, email="membership@a.edu", first_name="Member", last_name="Target")
    before = _state(a)
    original = issuance._Context.revalidate
    injected = False

    def drift(context):
        nonlocal injected
        if not injected:
            injected = True
            context.db.add(
                UserCredential(tenant_id=a.tid, party_id=target.id, password_hash=hash_password("injected-fixture9"))
            )
            context.db.flush()
        return original(context)

    monkeypatch.setattr(issuance._Context, "revalidate", drift)
    with pytest.raises(issuance.InvitationIssuanceError):
        _run(a, email=target.email, first_name="Ignored", last_name="Ignored", role="student")
    a.db.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert _state(a) == before


@pytest.mark.parametrize("restriction", ["valid", "revocation", "expiry", "role", "status", "inactive"])
def test_missing_resend_target_is_disclosed_only_after_fresh_actor_authorization(issuer, restriction):
    from fastapi import HTTPException

    from app.services.exceptions import NotFoundError

    a = issuer
    a.db.commit()
    if restriction == "role":
        _set_actor_role(a, "instructor")
    elif restriction == "status":
        a.admin.get(AcademyAccountProfile, a.pid).status = "suspended"
        a.admin.commit()
    elif restriction == "inactive":
        a.admin.get(Party, a.pid).is_active = False
        a.admin.commit()
    elif restriction in {"revocation", "expiry"}:
        session = a.admin.get(AuthSession, a.session_id)
        if restriction == "revocation":
            session.revoked_at = datetime.now(UTC)
        else:
            session.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        a.admin.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    before = _state(a)
    root = a.db.get_transaction()
    expected = NotFoundError if restriction == "valid" else HTTPException
    with pytest.raises(expected) as caught:
        with issuance.resend_web_account_invitation(a.db, request=_request(a.tenant, a.token), person_id=uuid4()):
            pytest.fail("missing target must never reach delivery")
    if restriction == "valid":
        assert str(caught.value) == "User not found"
    else:
        assert caught.value.status_code == (403 if restriction == "role" else 303)
        if restriction != "role":
            assert caught.value.headers == {"Location": "/login"}
    assert a.db.is_active and a.db.get_transaction() is root
    a.db.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert _state(a) == before


@pytest.mark.parametrize("is_active,status", [(False, "active"), (True, "suspended"), (False, "suspended")])
def test_initial_policy_survives_real_invitation_claim_and_denies_authentication(
    issuer, monkeypatch, is_active, status
):
    from app.services.credential_authentication import sign_in
    from app.services.invitation_claim import accept_invite

    a = issuer
    monkeypatch.setattr(settings_store.settings, "account_initial_is_active", is_active)
    monkeypatch.setattr(settings_store.settings, "account_initial_profile_status", status)
    result = _run(a, email=f"inactive-{uuid4().hex}@a.edu", first_name="Initial", last_name="State", role="student")
    assert result.created_person and result.person.is_active is is_active and result.person.status == status
    party = accept_invite(
        a.db,
        tenant_id=a.tid,
        raw=result.token,
        password="chosen-claim9",
        password_policy=invitation_acceptance_policy(a.db),
    )
    a.db.refresh(party)
    profile = a.db.get(AcademyAccountProfile, party.id)
    assert party.is_active is is_active and profile.status == status
    assert sign_in(a.db, tenant_id=a.tid, email=party.email, password="chosen-claim9") is None
    a.db.refresh(party)
    a.db.refresh(profile)
    assert party.is_active is is_active and profile.status == status


def test_initial_policy_does_not_rewrite_reused_account(issuer, monkeypatch):
    a = issuer
    existing = _person(
        a.db,
        tenant_id=a.tid,
        email=f"existing-{uuid4().hex}@a.edu",
        first_name="Existing",
        last_name="Person",
        status="paused",
        is_active=False,
    )
    monkeypatch.setattr(settings_store.settings, "account_initial_is_active", True)
    monkeypatch.setattr(settings_store.settings, "account_initial_profile_status", "active")
    result = _run(a, email=existing.email, first_name="Replace", last_name="Names", role="student")
    assert not result.created_person
    assert result.person.id == existing.id and result.person.is_active is False and result.person.status == "paused"


@pytest.mark.parametrize("field", ["is_active", "status"])
def test_new_invitation_state_tamper_rolls_back_after_caught_error(issuer, monkeypatch, field):

    a = issuer
    monkeypatch.setattr(settings_store.settings, "account_initial_is_active", False)
    monkeypatch.setattr(settings_store.settings, "account_initial_profile_status", "suspended")
    original = issuance._Context.audit
    contexts = []

    def tamper(context, result):
        contexts.append(context)
        if field == "is_active":
            context.target.is_active = True
        else:
            context.profile_rows[context.target_id].status = "active"
        context.db.flush()
        return original(context, result)

    monkeypatch.setattr(issuance._Context, "audit", tamper)
    email = f"tamper-{uuid4().hex}@a.edu"
    with pytest.raises(issuance.InvitationIssuanceError):
        _run(a, email=email, first_name="Tamper", last_name="State", role="student")
    assert contexts and all(context._initial_identity_policy is None for context in contexts)
    a.db.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    assert a.db.scalars(select(Party).where(Party.email == email)).first() is None
    assert a.admin.scalars(select(Party).where(Party.email == email)).first() is None


def test_invitation_settings_change_after_creation_preserves_pin_and_next_reads_new(issuer, monkeypatch):
    a = issuer
    monkeypatch.setattr(settings_store.settings, "account_initial_is_active", False)
    monkeypatch.setattr(settings_store.settings, "account_initial_profile_status", "suspended")
    original = issuance._Context.create_identity

    def change_after_creation(context, *args, **kwargs):
        original(context, *args, **kwargs)
        monkeypatch.setattr(settings_store.settings, "account_initial_is_active", True)
        monkeypatch.setattr(settings_store.settings, "account_initial_profile_status", "active")

    monkeypatch.setattr(issuance._Context, "create_identity", change_after_creation)
    first = _run(a, email=f"pinned-{uuid4().hex}@a.edu", first_name="First", last_name="State", role="student")
    second = _run(a, email=f"next-{uuid4().hex}@a.edu", first_name="Next", last_name="State", role="student")
    assert first.person.is_active is False and first.person.status == "suspended"
    assert second.person.is_active is True and second.person.status == "active"


@pytest.mark.parametrize("restriction", ["inactive", "suspended", "revoked", "expired", "role"])
def test_inactive_initial_policy_keeps_invitation_actor_checks(issuer, monkeypatch, restriction):
    from fastapi import HTTPException

    a = issuer
    monkeypatch.setattr(settings_store.settings, "account_initial_is_active", False)
    monkeypatch.setattr(settings_store.settings, "account_initial_profile_status", "suspended")
    a.db.commit()
    if restriction == "inactive":
        a.admin.get(Party, a.pid).is_active = False
    elif restriction == "suspended":
        a.admin.get(AcademyAccountProfile, a.pid).status = "suspended"
    elif restriction == "role":
        _set_actor_role(a, "student")
    else:
        session = a.admin.get(AuthSession, a.session_id)
        if restriction == "revoked":
            session.revoked_at = datetime.now(UTC)
        else:
            session.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    a.admin.commit()
    resolve_database_runtime().set_tenant(a.db, a.tid)
    before = _state(a)
    with pytest.raises(HTTPException):
        with issuance.invite_web_account(
            a.db,
            request=_request(a.tenant, a.token),
            email=f"denied-{uuid4().hex}@a.edu",
            first_name="Denied",
            last_name="State",
            role="student",
        ):
            pytest.fail("unauthorized actor reached delivery")
    assert _state(a) == before


@pytest.mark.parametrize("field", ["is_active", "status"])
def test_invitation_flush_state_verified_before_initial_snapshot(issuer, monkeypatch, field):
    from sqlalchemy import event, update

    a = issuer
    monkeypatch.setattr(settings_store.settings, "account_initial_is_active", False)
    monkeypatch.setattr(settings_store.settings, "account_initial_profile_status", "suspended")
    model = Party if field == "is_active" else AcademyAccountProfile

    def tamper(mapper, connection, target):
        if field == "is_active":
            connection.execute(update(Party).where(Party.id == target.id).values(is_active=True))
        else:
            connection.execute(
                update(AcademyAccountProfile)
                .where(AcademyAccountProfile.party_id == target.party_id)
                .values(status="active")
            )

    event.listen(model, "after_insert", tamper)
    before = _state(a)
    try:
        with pytest.raises(issuance.InvitationIssuanceError):
            _run(a, email=f"flush-{uuid4().hex}@a.edu", first_name="Flush", last_name="State", role="student")
    finally:
        event.remove(model, "after_insert", tamper)
    assert _state(a) == before


def test_real_settings_invitation_storage_preserves_server_defaults_and_existing_grant(issuer, monkeypatch):
    a = issuer
    role = a.db.scalars(select(Role).where(Role.tenant_id == a.tid, Role.slug == "student")).first()
    if role is None:
        role = Role(tenant_id=a.tid, slug="student", name="Tenant retained learner label")
        a.db.add(role)
        a.db.flush()
    else:
        role.name = "Tenant retained learner label"
        a.db.flush()
    role_id = role.id
    observer = f"observer-{uuid4().hex}"
    monkeypatch.setattr(settings_store.settings, "account_initial_is_active", False)
    monkeypatch.setattr(settings_store.settings, "account_initial_profile_status", "awaiting-review")
    monkeypatch.setattr(
        settings_store.settings,
        "web_account_creation_role_provisioning",
        {"student": "Changed configured default", observer: "Configured observer label"},
    )
    transaction_time = a.db.scalar(text("SELECT transaction_timestamp()"))
    email = f"storage-{uuid4().hex}@a.edu"
    first = _run(a, email=email, first_name="Actual", last_name="Invite", role="student")
    a.db.expire_all()
    party = a.db.get(Party, first.person.id)
    person = a.db.get(PartyPerson, first.person.id)
    profile = a.db.get(AcademyAccountProfile, first.person.id)
    grant = a.db.scalars(
        select(PartyRoleGrant).where(PartyRoleGrant.tenant_id == a.tid, PartyRoleGrant.party_id == first.person.id)
    ).one()
    original_grant = (grant.id, grant.tenant_id, grant.party_id, grant.role_id, grant.created_at, grant.updated_at)
    assert first.created_person and first.token is not None
    assert party.tenant_id == a.tid and party.email == email and party.display_name == "Actual Invite"
    assert party.is_active is False and party.custom_fields == {}
    assert (person.party_id, person.first_name, person.last_name) == (party.id, "Actual", "Invite")
    assert (profile.party_id, profile.tenant_id, profile.status) == (party.id, a.tid, "awaiting-review")
    assert profile.avatar_path is None and profile.prefs == {}
    assert (party.created_at, party.updated_at) == (profile.created_at, profile.updated_at)
    assert grant.id is not None and grant.role_id == role_id
    assert grant.created_at == transaction_time and grant.updated_at == transaction_time
    assert a.db.get(Role, role_id).name == "Tenant retained learner label"
    assert a.db.scalars(select(Role).where(Role.tenant_id == a.tid, Role.slug == observer)).one().name == "Configured observer label"
    assert a.db.scalars(select(UserCredential).where(UserCredential.party_id == party.id)).all() == []
    second = _run(a, email=email, first_name="Ignored", last_name="Ignored", role="student")
    a.db.expire_all()
    retained = a.db.scalars(
        select(PartyRoleGrant).where(PartyRoleGrant.tenant_id == a.tid, PartyRoleGrant.party_id == first.person.id)
    ).one()
    assert second.person.id == first.person.id and second.created_person is False
    assert (retained.id, retained.tenant_id, retained.party_id, retained.role_id, retained.created_at, retained.updated_at) == original_grant
    events = a.db.scalars(
        select(AuditEvent).where(
            AuditEvent.tenant_id == a.tid,
            AuditEvent.entity_id == str(party.id),
            AuditEvent.action == "account.invitation.completed",
        )
    ).all()
    assert len(events) == 2 and all(event.actor_id == str(a.pid) for event in events)


def test_genuine_role_uniqueness_interleaving_keeps_invitation_conflict_and_prior_work(issuer, monkeypatch):
    from app.services.exceptions import ConflictError

    a = issuer
    slug = f"racing-{uuid4().hex}"
    email = f"conflict-{uuid4().hex}@a.edu"
    prior = Role(tenant_id=a.tid, slug=f"prior-{uuid4().hex}", name="Prior caller work")
    a.db.add(prior)
    a.db.flush()
    prior_id = prior.id
    monkeypatch.setattr(settings_store.settings, "web_account_creation_role_provisioning", {slug: "Configured label"})
    original = issuance.provision_missing_roles
    winner_id = uuid4()

    def concurrent_insert(db, **values):
        a.admin.add(Role(id=winner_id, tenant_id=a.tid, slug=slug, name="Concurrent winner"))
        a.admin.commit()
        return original(db, **values)

    monkeypatch.setattr(issuance, "provision_missing_roles", concurrent_insert)
    root = a.db.get_transaction()
    with pytest.raises(ConflictError, match="invitation conflicts"):
        _run(a, email=email, first_name="Conflicting", last_name="Invite", role="student")
    assert a.db.is_active and a.db.get_transaction() is root
    a.db.commit()
    assert a.admin.get(Role, prior_id).name == "Prior caller work"
    winner = a.admin.scalars(select(Role).where(Role.tenant_id == a.tid, Role.slug == slug)).one()
    assert winner.id == winner_id and winner.name == "Concurrent winner"
    assert a.admin.scalars(select(Party).where(Party.tenant_id == a.tid, Party.email == email)).all() == []
