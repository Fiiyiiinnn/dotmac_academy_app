"""Who an enrolment is for — staff or external (ADR 0004).

The single writer of ``Enrollment.audience`` and ``Enrollment.employee_ref``.

This module **refuses to guess**. ERP owns employment, and identity between the
two systems is the employee reference, never a lowercased email match. The
Academy has no ERP read path today — the integration is an outbound webhook —
so classification is driven by an explicit roster supplied by ERP, and anything
not on that roster stays NULL rather than being defaulted to ``external``.

That is the point. 164 of 200 enrolments are staff and the only signal
currently available is whether the address ends ``@dotmac.ng``, which
misclassifies a staff member using a personal address and an external learner
issued a work one. An unclassified row is a question we have not answered; a
wrongly-defaulted row is a wrong answer that looks like a right one.
"""

# Ported at new-composition genesis (learning-facing wave).

from __future__ import annotations

from uuid import UUID

from dotmac_kernel.models import Party, PartyPerson, PartyType
from dotmac_learning.participation_models import Enrollment
from sqlalchemy import func, select
from sqlalchemy.orm import Session

STAFF = "staff"
EXTERNAL = "external"
AUDIENCES = (STAFF, EXTERNAL)


def classify_from_roster(
    db: Session, *, tenant_id: UUID, roster: dict[str, str], mark_rest_external: bool = False
) -> dict[str, int]:
    """Apply an ERP roster (``{work_email: employee_ref}``) to active enrolments.

    Matches are marked ``staff`` and carry their employee reference. Non-matches
    are left NULL unless ``mark_rest_external`` is set explicitly — an operator
    asserting "this roster is complete", which is a claim only they can make.

    Idempotent: re-running with the same roster changes nothing.
    """
    normalised = {(email or "").strip().lower(): ref for email, ref in roster.items() if email and ref}
    counts = {"staff": 0, "external": 0, "unclassified": 0, "unchanged": 0}

    rows = db.execute(
        select(Enrollment, Party.email)
        .join(Party, (Party.id == Enrollment.person_id) & (Party.tenant_id == Enrollment.tenant_id))
        .where(Party.party_type == PartyType.person)
        .where(Enrollment.tenant_id == tenant_id)
        .where(Enrollment.status == "active")
        .where(Enrollment.role_in_cohort == "student")
    ).all()

    for enrolment, email in rows:
        ref = normalised.get((email or "").strip().lower())
        if ref is not None:
            if enrolment.audience == STAFF and enrolment.employee_ref == ref:
                counts["unchanged"] += 1
                continue
            enrolment.audience = STAFF
            enrolment.employee_ref = ref
            counts["staff"] += 1
        elif mark_rest_external:
            if enrolment.audience == EXTERNAL:
                counts["unchanged"] += 1
                continue
            enrolment.audience = EXTERNAL
            enrolment.employee_ref = None
            counts["external"] += 1
        else:
            counts["unclassified"] += 1
    db.flush()
    return counts


def unclassified(db: Session, *, tenant_id: UUID) -> list[tuple[str, str]]:
    """(email, cohort-less identity) for active student enrolments with no audience.

    The human review queue ADR 0004 asks for — surfaced rather than guessed at.
    """
    rows = db.execute(
        select(Party.email, PartyPerson.first_name, PartyPerson.last_name)
        .select_from(Party)
        .join(PartyPerson, PartyPerson.party_id == Party.id)
        .join(Enrollment, (Enrollment.person_id == Party.id) & (Enrollment.tenant_id == Party.tenant_id))
        .where(Party.party_type == PartyType.person)
        .where(Enrollment.tenant_id == tenant_id)
        .where(Enrollment.status == "active")
        .where(Enrollment.role_in_cohort == "student")
        .where(Enrollment.audience.is_(None))
        .distinct()
        .order_by(Party.email)
    ).all()
    return [(email, f"{first or ''} {last or ''}".strip()) for email, first, last in rows]


def counts_by_audience(db: Session, *, tenant_id: UUID) -> dict[str, int]:
    """How the active student roster currently splits, including unclassified."""
    out = {STAFF: 0, EXTERNAL: 0, "unclassified": 0}
    for audience, n in db.execute(
        select(Enrollment.audience, func.count(func.distinct(Enrollment.person_id)))
        .where(Enrollment.tenant_id == tenant_id)
        .where(Enrollment.status == "active")
        .where(Enrollment.role_in_cohort == "student")
        .group_by(Enrollment.audience)
    ).all():
        out[audience or "unclassified"] = int(n)
    return out


def staff_person_ids(db: Session, *, tenant_id: UUID) -> list[UUID]:
    """People with at least one active enrolment explicitly marked staff.

    Only explicit marks count. An unclassified learner is never reported to HR
    as staff, because we do not know that they are.
    """
    return list(
        db.scalars(
            select(Enrollment.person_id)
            .where(Enrollment.tenant_id == tenant_id)
            .where(Enrollment.status == "active")
            .where(Enrollment.role_in_cohort == "student")
            .where(Enrollment.audience == STAFF)
            .distinct()
        ).all()
    )
