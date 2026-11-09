"""Authored hosted canaries for purpose-specific canonical learner reads.

These require the genuine full assembly, app_user RLS role and migrated owners;
this source capsule does not itself install or activate that assembly.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from dotmac_kernel.models import Party, PartyPerson
from sqlalchemy import delete, select, text, update

from app.models.cohort import Cohort, Enrollment
from app.models.identity_extensions import AcademyAccountProfile
from app.services import learner_digest, roster, success_queue
from app.services.exceptions import BadRequestError, NotFoundError
from app.services.facilitator_nominations import FacilitatorScope, NominateFacilitatorCommand, nominate
from app.services.roles import role_slugs
from legacy_probe.services.roster_request_fixtures import roster_actors
from legacy_probe.services.test_canonical_current_user import current_account
from legacy_probe.test_canonical_learning_identity_reads import write_free_identity_read

assert roster_actors
assert current_account


def _cohort(a, *, person=True, role="student", status="active"):
    cohort = Cohort(tenant_id=a.tid, name="Same raw <label>", discipline="networking", status="active")
    a.admin.add(cohort)
    a.admin.flush()
    if person:
        a.admin.add(
            Enrollment(tenant_id=a.tid, cohort_id=cohort.id, person_id=a.pid, role_in_cohort=role, status=status)
        )
    a.admin.commit()
    return cohort.id


def _digest_effects(monkeypatch):
    recipients, queued = [], []

    def build(db, *, tenant_id, person, since, now):
        recipients.append(person)
        return {"name": learner_digest._display_name(person), "headline": "A week", "courses": [], "more_courses": 0}

    monkeypatch.setattr(learner_digest, "build_digest", build)
    monkeypatch.setattr(learner_digest, "enqueue_email", lambda db, **kw: queued.append(kw) or True)
    return recipients, queued


def test_digest_distinct_person_across_duplicate_cohort_labels_current_prefs_email_and_name(
    current_account, monkeypatch
):
    a = current_account
    _cohort(a)
    _cohort(a)
    cached = a.db.get(Party, a.pid), a.db.get(PartyPerson, a.pid), a.db.get(AcademyAccountProfile, a.pid)
    updated_email = f"digest-{uuid4().hex}@a.edu"
    a.admin.execute(update(Party).where(Party.id == a.pid).values(email=updated_email))
    a.admin.execute(update(PartyPerson).where(PartyPerson.party_id == a.pid).values(first_name=" <Current> "))
    a.admin.execute(
        update(AcademyAccountProfile).where(AcademyAccountProfile.party_id == a.pid).values(prefs={"nested": {"x": 1}})
    )
    a.admin.commit()
    recipients, queued = _digest_effects(monkeypatch)
    now = datetime(2026, 10, 18, tzinfo=UTC)
    result = learner_digest.send_weekly_digests(a.db, tenant_id=a.tid, base_url="https://academy.example", now=now)
    assert result == {"queued": 1, "skipped_optout": 0, "skipped_no_courses": 0}
    assert len(recipients) == len(queued) == 1
    assert recipients[0].id == a.pid and recipients[0].prefs == {"nested": {"x": 1}}
    assert queued[0]["recipient"] == updated_email and "&lt;Current&gt;" in queued[0]["html_body"]
    assert queued[0]["idempotency_key"] == f"learner-digest:{now:%G-W%V}:{a.pid}"
    assert cached[0].email == a.email and cached[1].first_name == "Sign"


@pytest.mark.parametrize("status,active", [("active", True), ("suspended", True), ("active", False)])
def test_split_status_purpose_digest_and_success_exclude_but_bulk_lookup_accepts(
    current_account, monkeypatch, status, active, roster_actors
):
    a = current_account
    cid = _cohort(a)
    a.admin.execute(update(Party).where(Party.id == a.pid).values(is_active=active))
    a.admin.execute(update(AcademyAccountProfile).where(AcademyAccountProfile.party_id == a.pid).values(status=status))
    a.admin.commit()
    recipients, queued = _digest_effects(monkeypatch)
    learner_digest.send_weekly_digests(a.db, tenant_id=a.tid, base_url="https://academy.example")
    selected = list(a.db.scalars(success_queue._accounts(a.tid).active_person_ids))
    eligible = status == "active" and active
    assert (a.pid in selected) is eligible and bool(recipients) is eligible and bool(queued) is eligible
    result = roster.bulk_enroll(a.db, tenant_id=a.tid, cohort_id=cid, emails=[a.email], request=roster_actors.instructor.request)
    assert result["already_active"] == [a.email] and result["not_found"] == []


def test_digest_profile_optout_stops_before_build_and_enqueue(current_account, monkeypatch):
    a = current_account
    _cohort(a)
    a.admin.execute(
        update(AcademyAccountProfile)
        .where(AcademyAccountProfile.party_id == a.pid)
        .values(prefs={"email_learner_digest": False})
    )
    a.admin.commit()
    recipients, queued = _digest_effects(monkeypatch)
    result = learner_digest.send_weekly_digests(a.db, tenant_id=a.tid, base_url="https://academy.example")
    assert result == {"queued": 0, "skipped_optout": 1, "skipped_no_courses": 0}
    assert recipients == queued == []


@pytest.mark.parametrize("missing", ["profile", "subtype"])
def test_absent_canonical_part_is_excluded_or_refused_by_nomination_identity_checks(
    current_account, monkeypatch, missing, roster_actors
):
    a = current_account
    cid = _cohort(a)
    a.admin.execute(delete(AcademyAccountProfile).where(AcademyAccountProfile.party_id == a.pid))
    if missing == "subtype":
        a.admin.execute(delete(PartyPerson).where(PartyPerson.party_id == a.pid))
    a.admin.commit()
    assert a.pid not in a.db.scalars(success_queue._accounts(a.tid).active_person_ids).all()
    recipients, queued = _digest_effects(monkeypatch)
    learner_digest.send_weekly_digests(a.db, tenant_id=a.tid, base_url="https://academy.example")
    assert recipients == queued == []
    result = roster.bulk_enroll(a.db, tenant_id=a.tid, cohort_id=cid, emails=[a.email], request=roster_actors.instructor.request)
    assert result["not_found"] == [a.email] and result["enrolled"] == []
    # Nomination identity checks (#188): the nominee must resolve to a genuine
    # person party + subtype (404), and an account with no active profile is
    # not an active user (400). The retired roster writer joined the profile
    # into its existence check, so both absences were 404 there.
    command = NominateFacilitatorCommand(
        tenant_id=a.tid,
        person_id=a.pid,
        actor_person_id=roster_actors.admin.pid,
        scope=FacilitatorScope.COHORT,
        cohort_id=cid,
    )
    if missing == "subtype":
        with pytest.raises(NotFoundError, match="person not found for tenant"):
            nominate(a.db, command=command)
    else:
        with pytest.raises(BadRequestError, match="facilitator nominee must be an active user"):
            nominate(a.db, command=command)


def test_nomination_of_suspended_account_refuses_as_bad_request(current_account, roster_actors):
    a = current_account
    cid = _cohort(a, person=False)
    a.admin.execute(update(AcademyAccountProfile).where(AcademyAccountProfile.party_id == a.pid).values(status="suspended"))
    a.admin.commit()
    with pytest.raises(BadRequestError, match="facilitator nominee must be an active user"):
        nominate(
            a.db,
            command=NominateFacilitatorCommand(
                tenant_id=a.tid,
                person_id=a.pid,
                actor_person_id=roster_actors.admin.pid,
                scope=FacilitatorScope.COHORT,
                cohort_id=cid,
            ),
        )
    assert a.db.scalar(select(Enrollment.id).where(Enrollment.cohort_id == cid)) is None


def test_bulk_trim_case_dedup_and_nonstudent_reactivation_guard_keep_original_effects(current_account, roster_actors):
    a = current_account
    cid = _cohort(a, role="instructor", status="dropped")
    a.admin.execute(update(Party).where(Party.id == a.pid).values(email=f"  {a.email.upper()}  "))
    a.admin.commit()
    result = roster.bulk_enroll(
        a.db,
        tenant_id=a.tid,
        cohort_id=cid,
        emails=[f" {a.email.upper()} ", a.email, "", "missing@a.edu"],
        request=roster_actors.instructor.request,
    )
    assert result["admin_required"] == [a.email] and result["not_found"] == ["missing@a.edu"]
    enrollment = a.db.scalars(
        select(Enrollment).where(Enrollment.cohort_id == cid, Enrollment.person_id == a.pid)
    ).one()
    assert enrollment.status == "dropped" and enrollment.role_in_cohort == "instructor"


def test_cross_tenant_account_selection_and_digest_return_no_foreign_ids(current_account, tenant_b, monkeypatch):
    a = current_account
    _cohort(a)
    recipients, queued = _digest_effects(monkeypatch)
    # The Kernel grants app_user write privileges on parties by design (RLS isolates
    # tenants); the read paths are proven write-free in a READ ONLY transaction.
    with write_free_identity_read(a):
        assert a.pid not in a.db.scalars(success_queue._accounts(tenant_b.id).active_person_ids).all()
        result = learner_digest.send_weekly_digests(a.db, tenant_id=tenant_b.id, base_url="https://academy.example")
        assert result == {"queued": 0, "skipped_optout": 0, "skipped_no_courses": 0}
    assert recipients == queued == []
    assert a.db.scalar(text("SELECT current_user")) == "app_user"


def test_success_segment_delegation_keeps_canonical_account_relation_and_raw_member_order(current_account, monkeypatch):
    a = current_account
    cid = uuid4()
    members = (SimpleNamespace(id=uuid4(), last_name="Z"), SimpleNamespace(id=a.pid, last_name=" A "))
    captured = []
    monkeypatch.setattr(success_queue, "success_terms", lambda db: object())

    def owner(db, **kw):
        captured.append(kw)
        assert a.pid in db.scalars(kw["accounts"].active_person_ids).all()
        return members

    monkeypatch.setattr(success_queue.learning, "segment_members", owner)
    assert success_queue.segment_members(a.db, tenant_id=a.tid, cohort_id=cid, segment="inactive") == list(members)
    assert captured[0]["cohort_id"] == cid and captured[0]["accounts"].tenant_id == a.tid


def test_nomination_without_instructor_role_creates_no_enrollment(current_account, roster_actors):
    # #188 inversion of the retired writer's role gate: any active user may be
    # nominated, and nomination never writes an enrollment.
    a = current_account
    cid = _cohort(a, person=False)
    assert "instructor" not in role_slugs(a.db, a.tid, a.pid)
    outcome = nominate(
        a.db,
        command=NominateFacilitatorCommand(
            tenant_id=a.tid,
            person_id=a.pid,
            actor_person_id=roster_actors.admin.pid,
            scope=FacilitatorScope.COHORT,
            cohort_id=cid,
        ),
    )
    assert outcome.status.value == "pending"
    assert a.db.scalar(select(Enrollment.id).where(Enrollment.cohort_id == cid)) is None
