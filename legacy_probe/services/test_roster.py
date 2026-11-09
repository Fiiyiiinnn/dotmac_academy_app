"""Roster service: bulk enroll + roster-state transitions (Slice 3a)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from dotmac_kernel.models import Party, PartyPerson, PartyType
from sqlalchemy import select

from app.models.cohort import Cohort, Enrollment
from app.models.identity_extensions import AcademyAccountProfile
from app.services.exceptions import NotFoundError
from app.services.roster import bulk_enroll, set_roster_state
from legacy_probe.services.roster_request_fixtures import roster_actors

assert roster_actors


def _cohort(db, tid):
    coh = Cohort(tenant_id=tid, name="C", discipline="networking", status="active")
    db.add(coh)
    db.flush()
    return coh


def _person(db, tid, email):
    p = Party(tenant_id=tid, party_type=PartyType.person, email=email, display_name="P X", is_active=True)
    db.add(p)
    db.flush()
    db.add(PartyPerson(party_id=p.id, first_name="P", last_name="X"))
    db.flush()
    db.add(
        AcademyAccountProfile(
            tenant_id=tid,
            party_id=p.id,
            status="active",
            prefs={},
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    db.flush()
    return p


def test_bulk_enroll_reports_each_email(admin_session, tenant_a, roster_actors):
    admin_session = roster_actors.db
    tid = tenant_a.id
    coh = _cohort(admin_session, tid)
    _person(admin_session, tid, "a@x.edu")
    _person(admin_session, tid, "b@x.edu")

    res = bulk_enroll(
        admin_session,
        tenant_id=tid,
        cohort_id=coh.id,
        emails=["a@x.edu", "B@x.edu", "ghost@x.edu", "a@x.edu"],
        request=roster_actors.instructor.request,
    )
    assert sorted(res["enrolled"]) == ["a@x.edu", "b@x.edu"]  # dedup + case-insensitive
    assert res["not_found"] == ["ghost@x.edu"]

    n = (
        admin_session.query(Enrollment)
        .filter(Enrollment.tenant_id == tid, Enrollment.cohort_id == coh.id, Enrollment.status == "active")
        .count()
    )
    assert n == 2

    # Idempotent re-run reports already_active.
    res2 = bulk_enroll(
        admin_session, tenant_id=tid, cohort_id=coh.id, emails=["a@x.edu"], request=roster_actors.instructor.request
    )
    assert res2["already_active"] == ["a@x.edu"]
    admin_session.rollback()


def test_set_roster_state_drop_and_reactivate(admin_session, tenant_a, roster_actors):
    admin_session = roster_actors.db
    tid = tenant_a.id
    coh = _cohort(admin_session, tid)
    p = _person(admin_session, tid, "c@x.edu")
    bulk_enroll(
        admin_session, tenant_id=tid, cohort_id=coh.id, emails=["c@x.edu"], request=roster_actors.instructor.request
    )

    set_roster_state(
        admin_session,
        tenant_id=tid,
        cohort_id=coh.id,
        person_id=p.id,
        state="dropped",
        request=roster_actors.instructor.request,
    )
    enr = admin_session.scalars(
        __import__("sqlalchemy")
        .select(Enrollment)
        .where(Enrollment.cohort_id == coh.id)
        .where(Enrollment.person_id == p.id)
    ).first()
    assert enr.status == "dropped"

    # bulk_enroll reactivates a dropped STUDENT member regardless of
    # actor_is_admin — the admin gate only applies to non-student enrollments.
    res = bulk_enroll(
        admin_session, tenant_id=tid, cohort_id=coh.id, emails=["c@x.edu"], request=roster_actors.instructor.request
    )
    assert res["reactivated"] == ["c@x.edu"]
    admin_session.rollback()


def test_invalid_state_and_missing_enrollment_raise(admin_session, tenant_a, roster_actors):
    admin_session = roster_actors.db
    tid = tenant_a.id
    coh = _cohort(admin_session, tid)
    p = _person(admin_session, tid, "d@x.edu")
    with pytest.raises(NotFoundError):
        set_roster_state(
            admin_session,
            tenant_id=tid,
            cohort_id=coh.id,
            person_id=p.id,
            state="bogus",
            request=roster_actors.instructor.request,
        )
    with pytest.raises(NotFoundError):
        set_roster_state(
            admin_session,
            tenant_id=tid,
            cohort_id=coh.id,
            person_id=p.id,
            state="dropped",
            request=roster_actors.instructor.request,
        )
    admin_session.rollback()


def test_bulk_enroll_reactivating_non_student_enrollment_is_admin_gated(admin_session, tenant_a, roster_actors):
    admin_session = roster_actors.db
    """The privilege-restoration gap this fix closes: bulk_enroll must refuse
    to reactivate a dropped non-student (e.g. instructor-role) enrollment for
    a non-admin actor — categorized into "admin_required" (bulk_enroll's
    existing per-email-outcome contract, matching "not_found"), NOT a raised
    exception — and succeed (categorized "reactivated") for an admin actor."""
    tid = tenant_a.id
    coh = _cohort(admin_session, tid)
    p = _person(admin_session, tid, "instr@x.edu")
    enrollment = Enrollment(
        tenant_id=tid,
        cohort_id=coh.id,
        person_id=p.id,
        role_in_cohort="instructor",
        status="dropped",
    )
    admin_session.add(enrollment)
    admin_session.flush()

    res = bulk_enroll(
        admin_session, tenant_id=tid, cohort_id=coh.id, emails=["instr@x.edu"], request=roster_actors.instructor.request
    )
    assert res["admin_required"] == ["instr@x.edu"]
    assert res["reactivated"] == []
    admin_session.refresh(enrollment)
    assert enrollment.status == "dropped"  # unchanged after the refusal

    res2 = bulk_enroll(
        admin_session, tenant_id=tid, cohort_id=coh.id, emails=["instr@x.edu"], request=roster_actors.admin.request
    )
    assert res2["reactivated"] == ["instr@x.edu"]
    assert res2["admin_required"] == []
    admin_session.refresh(enrollment)
    assert enrollment.status == "active"
    admin_session.rollback()


def test_bulk_enroll_continues_batch_after_a_refused_reactivation(admin_session, tenant_a, roster_actors):
    admin_session = roster_actors.db
    """A refused reactivation for one email must not abort the rest of the
    same call's batch — proves no unintended abort-on-first-refusal behavior."""
    tid = tenant_a.id
    coh = _cohort(admin_session, tid)
    peer = _person(admin_session, tid, "peer@x.edu")
    fresh = _person(admin_session, tid, "fresh@x.edu")
    enrollment = Enrollment(
        tenant_id=tid,
        cohort_id=coh.id,
        person_id=peer.id,
        role_in_cohort="instructor",
        status="dropped",
    )
    admin_session.add(enrollment)
    admin_session.flush()

    res = bulk_enroll(
        admin_session,
        tenant_id=tid,
        cohort_id=coh.id,
        emails=["peer@x.edu", "fresh@x.edu"],
        request=roster_actors.instructor.request,
    )
    assert res["admin_required"] == ["peer@x.edu"]
    assert res["enrolled"] == ["fresh@x.edu"]

    admin_session.refresh(enrollment)
    assert enrollment.status == "dropped"  # refused, unchanged
    fresh_enr = admin_session.scalars(
        select(Enrollment).where(Enrollment.cohort_id == coh.id).where(Enrollment.person_id == fresh.id)
    ).first()
    assert fresh_enr is not None and fresh_enr.status == "active"  # rest of the batch still applied
    admin_session.rollback()


@pytest.mark.parametrize("state", ["active", "waitlisted", "dropped"])
@pytest.mark.parametrize("actor_is_admin", [False, True])
def test_state_adapter_forwards_exact_command_authority_and_detached_owner_fact(
    monkeypatch, state, actor_is_admin, roster_actors
):
    from datetime import UTC, datetime
    from unittest.mock import Mock
    from uuid import uuid4

    import dotmac_learning as learning

    from app.services import roster

    tenant, cohort, person = roster_actors.account.tid, uuid4(), roster_actors.account.pid
    now = datetime(2026, 10, 18, tzinfo=UTC)
    fact = learning.EnrollmentFact(uuid4(), tenant, cohort, person, None, "student", state, None, None, None, now, now)
    owner = Mock(return_value=fact)
    monkeypatch.setattr(learning, "set_roster_state", owner)
    db = roster_actors.db
    for method in ("scalars", "flush", "commit", "rollback"):
        monkeypatch.setattr(db, method, Mock(wraps=getattr(db, method)))
    actor = roster_actors.admin if actor_is_admin else roster_actors.instructor
    result = roster.set_roster_state(
        db, tenant_id=tenant, cohort_id=cohort, person_id=person, state=state, request=actor.request
    )
    assert result is fact
    owner.assert_called_once_with(
        db,
        learning.RosterStateCommand(tenant_id=tenant, cohort_id=cohort, person_id=person, state=state),
        authority=learning.RosterAuthority(tenant_id=tenant, actor_is_admin=actor_is_admin),
    )
    db.scalars.assert_called()
    db.flush.assert_called()
    db.commit.assert_not_called()
    db.rollback.assert_not_called()


@pytest.mark.parametrize("detail", ["invalid roster state: bogus", "enrollment not found"])
def test_state_adapter_preserves_typed_owner_not_found_detail(monkeypatch, detail, roster_actors):
    from unittest.mock import Mock
    from uuid import uuid4

    import dotmac_learning as learning

    from app.services import roster

    owner = Mock(side_effect=learning.ParticipationNotFound(detail))
    monkeypatch.setattr(learning, "set_roster_state", owner)
    with pytest.raises(NotFoundError) as caught:
        roster.set_roster_state(
            roster_actors.db,
            tenant_id=roster_actors.account.tid,
            cohort_id=uuid4(),
            person_id=uuid4(),
            state="bogus",
            request=roster_actors.instructor.request,
        )
    assert str(caught.value) == detail
    assert caught.value.__suppress_context__ is True
    owner.assert_called_once()


def test_state_adapter_preserves_forbidden_and_required_failures(monkeypatch, roster_actors):
    from unittest.mock import Mock
    from uuid import uuid4

    import dotmac_learning as learning
    from fastapi import HTTPException

    from app.services import roster
    from app.services.audit_context import HumanOperationError

    db = roster_actors.db
    for method in ("flush", "commit", "rollback"):
        monkeypatch.setattr(db, method, Mock(wraps=getattr(db, method)))
    kwargs = dict(
        tenant_id=roster_actors.account.tid,
        cohort_id=uuid4(),
        person_id=uuid4(),
        state="dropped",
        request=roster_actors.instructor.request,
    )
    owner = Mock(side_effect=learning.ParticipationForbidden("Forbidden"))
    monkeypatch.setattr(learning, "set_roster_state", owner)
    with pytest.raises(HTTPException) as caught:
        roster.set_roster_state(db, **kwargs)
    assert caught.value.status_code == 403 and caught.value.detail == "Forbidden"
    required = RuntimeError("required row observation failure")
    owner.side_effect = required
    with pytest.raises(HumanOperationError) as failure:
        roster.set_roster_state(db, **kwargs)
    assert failure.value.__context__ is required
    db.flush.assert_called()
    db.commit.assert_not_called()
    db.rollback.assert_not_called()


def test_state_adapter_requires_explicit_authority_before_calling_owner(monkeypatch):
    from unittest.mock import Mock
    from uuid import uuid4

    import dotmac_learning as learning

    from app.services import roster

    owner = Mock()
    monkeypatch.setattr(learning, "set_roster_state", owner)
    with pytest.raises(TypeError, match="request"):
        roster.set_roster_state(object(), tenant_id=uuid4(), cohort_id=uuid4(), person_id=uuid4(), state="dropped")
    owner.assert_not_called()
