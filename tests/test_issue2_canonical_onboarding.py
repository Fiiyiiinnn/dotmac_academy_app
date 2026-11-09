"""Issue #2 port of legacy services/test_canonical_onboarding.py.

Disposition: test_onboarding_bearer_atomic_zero_effect_on_failure
against app/services/invitation_issuance.py finish_onboarding_step.
Ports the genuine bearer enrollment + system audits + repeat test and
the invalid/rotated/cross-tenant zero-effects test, plus the enqueue
stage-failure atomicity case. Remaining policy-pinning matrix stays
in legacy_probe.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from dotmac_kernel.audit import AuditEvent
from dotmac_kernel.models import Party, PartyRoleGrant
from dotmac_kernel.session_runtime import resolve_database_runtime
from dotmac_learning.participation_models import Cohort, CohortTrack, Enrollment, Track
from sqlalchemy import select, text

from app.models.account_token import AccountToken
from app.models.admissions import Applicant
from app.models.email_outbox import EmailOutbox
from app.models.onboarding import OnboardingTask
from app.services import admissions, onboarding
from app.services import invitation_issuance as issuance
from app.services.security import hash_token
from tests.issue2_wave1_fixtures import (  # noqa: F401 - pytest fixtures
    admin_engine,
    admin_session,
    app_client,
    app_user_session,
    canonical_account,
    current_account,
    invitation_account,
    lab_policy_rows,
    lab_worker_engine,
    lab_worker_session,
    no_real_console_spawn,
    policy,
    seed_lab,
    sign_in_account,
    tenant_a,
    tenant_b,
)

assert current_account


@pytest.fixture
def onboarding_case(current_account):
    a = current_account
    raw = uuid4().hex + uuid4().hex
    cohort = Cohort(tenant_id=a.tid, name="Bearer [REDACTED]", discipline="fiber", status="active")
    track = Track(tenant_id=a.tid, name="Bearer [REDACTED]", slug=uuid4().hex, status="active")
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


def test_onboarding_bearer_atomic_zero_effect_on_failure(onboarding_case):
    c = onboarding_case

    # Invalid and cross-tenant bearers: zero effects.
    before = _snapshot(c)
    assert _finish(c, raw="invalid") is None
    assert _snapshot(c) == before
    assert _finish(c, raw=c.raw, tenant_id=uuid4()) is None
    assert _snapshot(c) == before

    # Failure inside the caller's enqueue, caught, then an outer commit
    # still preserves zero effects.
    failure = RuntimeError("injected stage failure")

    def enqueue(result):
        from app.services import applicant_email

        applicant_email.send_enrollment_welcome(
            c.a.db,
            applicant=result.applicant,
            setup_url=f"https://academy.example/accept-invite?token={result.invite_raw}",
        )
        raise failure

    root = c.a.db.get_transaction()
    with pytest.raises(RuntimeError) as caught:
        _finish(c, enqueue=enqueue)
    assert caught.value is failure and c.a.db.get_transaction() is root and c.a.db.is_active
    c.a.db.commit()
    resolve_database_runtime().set_tenant(c.a.db, c.a.tid)
    assert _snapshot(c) == before
    assert c.a.db.scalar(text("SELECT current_setting('app.current_tenant')")) == str(c.a.tid)

    # Genuine bearer: enrollment, one invite token, fixed system audits.
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

    # Repeat: no second enrollment, no second token.
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


def test_rotated_bearer_has_zero_effects(onboarding_case):
    c = onboarding_case
    applicant = c.a.db.get(Applicant, c.id)
    new_raw = admissions.mint_onboarding_token(c.a.db, applicant=applicant)
    before = _snapshot(c)
    assert _finish(c, raw=c.raw) is None
    assert _snapshot(c) == before
    # The rotated-in token is the live bearer.
    result = _finish(c, raw=new_raw)
    assert result is not None and result.enrolled_now
