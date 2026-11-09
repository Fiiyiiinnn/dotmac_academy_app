"""Staff/external classification and the HR roll-up (ADR 0004).

Requires a migrated disposable Postgres (skipped otherwise by the fixtures).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from dotmac_kernel.models import Party, PartyPerson, PartyType
from sqlalchemy import select

from app.models.auth import AuthSession, UserCredential
from app.models.cohort import Cohort, Enrollment
from app.models.email_outbox import EmailOutbox
from app.models.identity_extensions import AcademyAccountProfile, AcademyCredentialPolicy
from app.models.learning_event import LearningEvent
from app.services import audience, hr_digest
from app.services.security import hash_password


def _cohort(db, tenant, name="Aud"):
    c = Cohort(tenant_id=tenant.id, name=name, discipline="fiber", status="active")
    db.add(c)
    db.flush()
    return c


def _enrol(db, tenant, cohort, email, *, role="student", first="A", last="B"):
    now = datetime.now(UTC)
    p = Party(
        tenant_id=tenant.id, party_type=PartyType.person, display_name=f"{first} {last}",
        email=email, is_active=True,
    )
    db.add(p)
    db.flush()
    db.add(PartyPerson(party_id=p.id, first_name=first, last_name=last))
    db.flush()
    db.add(AcademyAccountProfile(
        tenant_id=tenant.id, party_id=p.id, status="active", prefs={}, created_at=now, updated_at=now,
    ))
    db.add(Enrollment(tenant_id=tenant.id, cohort_id=cohort.id, person_id=p.id,
                      role_in_cohort=role, status="active"))
    db.flush()
    return p


def test_roster_marks_staff_and_leaves_the_rest_unclassified(admin_session, tenant_a):
    """The core rule: no guessing. An address not on the roster is a question we
    have not answered, not an assertion that the learner is external."""
    cohort = _cohort(admin_session, tenant_a, "aud-roster")
    _enrol(admin_session, tenant_a, cohort, "staffer@dotmac.ng")
    _enrol(admin_session, tenant_a, cohort, "outsider@gmail.com")
    # A staff member on a personal address — the email-domain heuristic would
    # have got this one wrong in the other direction.
    _enrol(admin_session, tenant_a, cohort, "personal@gmail.com")
    admin_session.commit()

    counts = audience.classify_from_roster(
        admin_session, tenant_id=tenant_a.id,
        roster={"staffer@dotmac.ng": "EMP-1", "personal@gmail.com": "EMP-2"},
    )
    split = audience.counts_by_audience(admin_session, tenant_id=tenant_a.id)
    pending = audience.unclassified(admin_session, tenant_id=tenant_a.id)
    admin_session.rollback()

    assert counts["staff"] == 2
    assert counts["unclassified"] == 1
    assert split == {"staff": 2, "external": 0, "unclassified": 1}
    assert [e for e, _ in pending] == ["outsider@gmail.com"]


def test_classification_is_idempotent(admin_session, tenant_a):
    cohort = _cohort(admin_session, tenant_a, "aud-idem")
    _enrol(admin_session, tenant_a, cohort, "s@dotmac.ng")
    admin_session.commit()
    roster = {"s@dotmac.ng": "EMP-9"}

    first = audience.classify_from_roster(admin_session, tenant_id=tenant_a.id, roster=roster)
    second = audience.classify_from_roster(admin_session, tenant_id=tenant_a.id, roster=roster)
    admin_session.rollback()

    assert first["staff"] == 1
    assert second["staff"] == 0 and second["unchanged"] == 1


def test_rest_marked_external_only_when_asserted(admin_session, tenant_a):
    cohort = _cohort(admin_session, tenant_a, "aud-complete")
    _enrol(admin_session, tenant_a, cohort, "in@dotmac.ng")
    _enrol(admin_session, tenant_a, cohort, "out@gmail.com")
    admin_session.commit()

    counts = audience.classify_from_roster(
        admin_session, tenant_id=tenant_a.id, roster={"in@dotmac.ng": "EMP-3"},
        mark_rest_external=True,
    )
    split = audience.counts_by_audience(admin_session, tenant_id=tenant_a.id)
    admin_session.rollback()

    assert counts["staff"] == 1 and counts["external"] == 1
    assert split["unclassified"] == 0


def test_a_staff_row_must_carry_an_employee_ref(admin_session, tenant_a):
    """Identity between the systems is the reference, so the database enforces it."""
    import pytest
    from sqlalchemy.exc import IntegrityError

    cohort = _cohort(admin_session, tenant_a, "aud-ck")
    person = _enrol(admin_session, tenant_a, cohort, "ck@dotmac.ng")
    enrolment = admin_session.scalars(
        select(Enrollment).where(Enrollment.person_id == person.id)
    ).one()
    enrolment.audience = "staff"
    enrolment.employee_ref = None

    with pytest.raises(IntegrityError):
        admin_session.flush()
    admin_session.rollback()


def test_hr_report_counts_only_explicit_staff_and_states_the_gap(admin_session, tenant_a):
    """A roll-up that quietly omits half the roster invites HR to read it as complete."""
    now = datetime.now(UTC)
    cohort = _cohort(admin_session, tenant_a, "aud-hr")
    _enrol(admin_session, tenant_a, cohort, "hr-staff@dotmac.ng", first="Sta", last="Ff")
    _enrol(admin_session, tenant_a, cohort, "hr-unknown@gmail.com")
    admin_session.commit()
    audience.classify_from_roster(
        admin_session, tenant_id=tenant_a.id, roster={"hr-staff@dotmac.ng": "EMP-7"}
    )
    admin_session.commit()

    snap = hr_digest.snapshot(
        admin_session, tenant_id=tenant_a.id, since=now - timedelta(days=7), now=now
    )
    _html, text = hr_digest.render(snap)
    admin_session.rollback()

    assert snap["staff"] == 1  # the unclassified learner is not counted as staff
    assert snap["unclassified"] == 1
    assert snap["never_activated_total"] == 1  # staffer has no credential
    assert "Sta Ff" in text
    assert "not yet classified" in text


def test_hr_report_separates_never_activated_from_stalled(admin_session, tenant_a):
    """Different remedies: one cannot log in, the other chose not to study."""
    now = datetime.now(UTC)
    cohort = _cohort(admin_session, tenant_a, "aud-split")
    locked = _enrol(admin_session, tenant_a, cohort, "locked@dotmac.ng", first="Lo", last="Cked")
    quiet = _enrol(admin_session, tenant_a, cohort, "quiet@dotmac.ng", first="Qu", last="Iet")
    credential = UserCredential(
        tenant_id=tenant_a.id, party_id=quiet.id,
        password_hash=hash_password("correct horse battery staple"),
    )
    admin_session.add(credential)
    admin_session.flush()
    admin_session.add_all([
        AcademyCredentialPolicy(
            tenant_id=tenant_a.id, credential_id=credential.id, email=quiet.email,
            failed_login_attempts=0, locked_until=None, lifecycle_version=0,
            reset_required=False, created_at=now, updated_at=now,
        ),
        AuthSession(tenant_id=tenant_a.id, party_id=quiet.id, token_hash="b" * 64,
                    expires_at=now + timedelta(days=1)),
        LearningEvent(tenant_id=tenant_a.id, person_id=quiet.id, kind="chapter_viewed",
                      detail={}, occurred_at=now - timedelta(days=30)),
    ])
    admin_session.commit()
    audience.classify_from_roster(
        admin_session, tenant_id=tenant_a.id,
        roster={"locked@dotmac.ng": "EMP-A", "quiet@dotmac.ng": "EMP-B"},
    )
    admin_session.commit()

    snap = hr_digest.snapshot(
        admin_session, tenant_id=tenant_a.id, since=now - timedelta(days=7), now=now
    )
    admin_session.rollback()

    assert snap["never_activated"] == ["Lo Cked"]
    assert snap["stalled"] == ["Qu Iet"]
    assert snap["active_in_window"] == 0
    assert locked.id != quiet.id


def test_hr_report_is_idempotent_per_week(admin_session, tenant_a):
    cohort = _cohort(admin_session, tenant_a, "aud-once")
    _enrol(admin_session, tenant_a, cohort, "once@dotmac.ng")
    admin_session.commit()
    audience.classify_from_roster(
        admin_session, tenant_id=tenant_a.id, roster={"once@dotmac.ng": "EMP-Z"}
    )

    first = hr_digest.send_hr_report(admin_session, tenant_id=tenant_a.id, recipients=["hr@dotmac.ng"])
    second = hr_digest.send_hr_report(admin_session, tenant_id=tenant_a.id, recipients=["hr@dotmac.ng"])
    admin_session.commit()
    mails = list(
        admin_session.scalars(
            select(EmailOutbox)
            .where(EmailOutbox.tenant_id == tenant_a.id)
            .where(EmailOutbox.kind == "hr_training_report")
        )
    )
    admin_session.rollback()

    assert first == 1 and second == 0
    assert len(mails) == 1
