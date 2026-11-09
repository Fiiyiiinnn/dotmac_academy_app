"""Admissions service — application intake and pipeline transitions.

Follows the repo convention: functions take ``db`` + ``tenant_id`` explicitly,
``flush`` but never ``commit`` (the request/CLI owns the transaction), and raise
domain exceptions (``app/services/exceptions.py``) for the router to translate.
Tenant scoping is enforced by RLS; we still pass ``tenant_id`` on writes so the
``WITH CHECK`` policy passes.
"""

# Ported at new-composition genesis (admissions services slice).

from __future__ import annotations

import secrets
from datetime import UTC, date, datetime
from typing import cast
from uuid import UUID

import dotmac_learning as learning
from dotmac_learning.participation_models import Cohort, CohortTrack, Track
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.admissions import APPLICANT_STATUSES, Applicant
from app.services import onboarding
from app.services.admission_assessment_policy import assessment_path, below_threshold_reason
from app.services.exceptions import BadRequestError, ConflictError, NotFoundError
from app.services.security import hash_token


def _legacy_write_audit_event(*args, **kwargs):
    """Explicit automated legacy assessment origin, never a human fallback."""
    from app.services.audit import write_audit_event

    if kwargs.pop("actor_person_id", None) is not None:
        raise BadRequestError("legacy assessment origin cannot assert a human actor")
    return write_audit_event(
        *args,
        **kwargs,
        actor_type="system",
        actor_id="assessment_policy",
        actor_party_id=None,
        source_application="dotmac_academy_app",
    )


VALID_STATUSES = frozenset(APPLICANT_STATUSES)

ADMIN_ACTION_LABELS = {
    "accept": "Accept and invite to onboarding",
    "waitlist": "Move to waitlist",
    "reject": "Reject application",
    "resend_invitation": "Resend invitation",
    "extend_access": "Extend access",
    "reinvite_assessment": "Send a new assessment invitation",
    "reset_assessment": "Reset sitting and send a new invitation",
}

# Allowed forward/off-ramp transitions. Accept routes into ``onboarding`` (the
# onboarding workflow lands in P2); ``enrolled`` and ``rejected`` are terminal.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "applied": frozenset({"screened", "rejected", "waitlisted"}),
    "screened": frozenset({"accepted", "rejected", "waitlisted"}),
    "waitlisted": frozenset({"screened", "accepted", "rejected"}),
    "accepted": frozenset({"onboarding", "rejected"}),
    "onboarding": frozenset({"enrolled", "rejected"}),
    "enrolled": frozenset(),
    "rejected": frozenset(),
}


# Profile fields the application form may supply. Only these are accepted from a
# public form, so a crafted POST cannot write arbitrary columns.
PROFILE_FIELDS = (
    "date_of_birth",
    "state",
    "city",
    "highest_qualification",
    "field_of_study",
    "years_experience",
    "current_role",
    "has_device",
    "has_internet",
    "can_work_at_height",
    "available_from",
    "heard_from",
    "cv_url",
)


def _apply_profile(applicant: Applicant, profile: dict | None) -> None:
    """Copy supplied profile fields onto the applicant.

    A field the candidate left blank must never wipe one they already gave, so
    None is skipped rather than written.
    """
    if not profile:
        return
    for f in PROFILE_FIELDS:
        v = profile.get(f)
        if v is not None:
            setattr(applicant, f, v)


def submit_application(
    db: Session,
    *,
    tenant_id: UUID,
    email: str,
    first_name: str,
    last_name: str,
    phone: str | None = None,
    program: str | None = None,
    cohort_id: UUID | None = None,
    track_id: UUID | None = None,
    source: str = "website",
    external_ref: str | None = None,
    applied_on: date | None = None,
    profile: dict | None = None,
) -> Applicant:
    """Create (or refresh) an application. Idempotent on (tenant, email).

    A re-application updates the contact details but never regresses an
    in-flight applicant's pipeline status.
    """
    email = email.strip().lower()
    selected_track: Track | None = None
    if track_id is not None:
        if cohort_id is None:
            raise BadRequestError("A cohort is required when selecting a track.")
        selected_track = db.scalars(
            select(Track)
            .join(
                CohortTrack,
                (CohortTrack.tenant_id == Track.tenant_id) & (CohortTrack.track_id == Track.id),
            )
            .join(
                Cohort,
                (Cohort.tenant_id == CohortTrack.tenant_id) & (Cohort.id == CohortTrack.cohort_id),
            )
            .where(Track.tenant_id == tenant_id)
            .where(Track.id == track_id)
            .where(Track.status == "active")
            .where(CohortTrack.cohort_id == cohort_id)
            .where(CohortTrack.status == "active")
            .where(Cohort.status == "active")
        ).first()
        if selected_track is None:
            raise BadRequestError("That training track is not open for the selected cohort.")
        program = selected_track.name
    lookup = (
        select(Applicant).where(Applicant.external_ref == external_ref)
        if external_ref
        else select(Applicant).where(Applicant.email == email).where(Applicant.external_ref.is_(None))
    )
    existing = db.scalar(lookup)
    if existing is not None:
        existing.first_name = first_name.strip()
        existing.last_name = last_name.strip()
        if phone:
            existing.phone = phone.strip()
        if program:
            existing.program = program.strip()
        if cohort_id is not None:
            existing.cohort_id = cohort_id
        if selected_track is not None:
            existing.track_id = selected_track.id
        if external_ref and not existing.external_ref:
            existing.external_ref = external_ref
        _apply_profile(existing, profile)
        db.flush()
        return existing

    applicant = Applicant(
        tenant_id=tenant_id,
        email=email,
        first_name=first_name.strip(),
        last_name=last_name.strip(),
        phone=phone.strip() if phone else None,
        program=program.strip() if program else None,
        cohort_id=cohort_id,
        track_id=selected_track.id if selected_track is not None else None,
        status="applied",
        source=source,
        external_ref=external_ref,
        applied_on=applied_on or date.today(),
    )
    _apply_profile(applicant, profile)
    db.add(applicant)
    try:
        db.flush()
    except IntegrityError as exc:  # concurrent submit for the same email
        db.rollback()
        raise ConflictError("An application with this email already exists.") from exc
    return applicant


def submit_external_application(
    db: Session,
    *,
    tenant_id: UUID,
    external_ref: str,
    email: str,
    first_name: str,
    last_name: str,
    source: str,
    applied_on: date | None = None,
) -> tuple[Applicant, bool]:
    """Create (or find) an applicant owned by an external system's application.

    The second identity rule for intake, and the reason this belongs here rather
    than in the caller. ``submit_application`` is idempotent on *(tenant, email)*
    — one applicant per person — which is right for public intake. An external
    system's applications are not people: the same individual may apply for two
    jobs and must get two applicants, two sittings and two results. So this is
    idempotent on *(tenant, external_ref)* instead.

    Both rules are intake decisions, so both live with the intake owner. A
    caller that inserted ``Applicant`` rows itself to get the second rule would
    be a second writer of intake, and the two would drift the moment either
    changed — the failure ``docs/adr/0005-assessment-engine-owns-exam-logic.md``
    records one layer up.

    Returns the applicant and whether this call created it, because callers
    need to distinguish a first registration from a replay.
    """
    canonical = email.strip().lower()

    created = db.execute(
        insert(Applicant)
        .values(
            tenant_id=tenant_id,
            email=canonical,
            first_name=first_name.strip(),
            last_name=last_name.strip(),
            status="applied",
            source=source,
            external_ref=external_ref,
            applied_on=applied_on or date.today(),
        )
        .on_conflict_do_nothing(
            index_elements=[Applicant.tenant_id, Applicant.external_ref],
            index_where=Applicant.external_ref.is_not(None),
        )
        .returning(Applicant.id)
    ).first()

    applicant = db.scalars(
        select(Applicant)
        .where(Applicant.tenant_id == tenant_id)
        .where(Applicant.external_ref == external_ref)
        .with_for_update()
    ).first()
    if applicant is None:
        # Insert did nothing and the row is not there: the external_ref lost a
        # race with a conflicting write, or the partial index is missing.
        raise ConflictError("Could not establish an applicant for that external reference.")

    if applicant.email != canonical:
        raise ConflictError("That external reference is already registered to a different email address.")

    return applicant, created is not None


def list_applicants(
    db: Session,
    *,
    status: str | None = None,
    cohort_id: UUID | None = None,
    search: str | None = None,
    applied_from: date | None = None,
    applied_to: date | None = None,
    rank_by_score: bool = False,
    include_invalid: bool = False,
    limit: int | None = None,
    offset: int = 0,
) -> list[Applicant]:
    """List applicants (RLS scopes to the current tenant).

    ``cohort_id`` filters to one intake's candidates; ``rank_by_score`` orders
    by entrance-assessment score (best first, un-assessed last) for selection.

    When ranking, sittings that failed the validity gate (``assessment_valid`` is
    false — near-chance score, or submitted too fast to have engaged) are EXCLUDED
    by default. Those carry no signal: ranking them would seat, or reject, people
    on the strength of random clicking. Pass ``include_invalid=True`` to see them
    anyway (e.g. to review who needs a reset).
    """
    stmt = select(Applicant)
    if status is not None:
        if status not in VALID_STATUSES:
            raise BadRequestError(f"Unknown status: {status}")
        stmt = stmt.where(Applicant.status == status)
    if cohort_id is not None:
        stmt = stmt.where(Applicant.cohort_id == cohort_id)
    if search and (term := search.strip()):
        pattern = f"%{term}%"
        stmt = stmt.where(
            or_(
                Applicant.first_name.ilike(pattern),
                Applicant.last_name.ilike(pattern),
                (Applicant.first_name + " " + Applicant.last_name).ilike(pattern),
            )
        )
    if applied_from is not None:
        stmt = stmt.where(Applicant.applied_on >= applied_from)
    if applied_to is not None:
        stmt = stmt.where(Applicant.applied_on <= applied_to)
    if rank_by_score:
        if not include_invalid:
            stmt = stmt.where(Applicant.assessment_valid.is_not(False))
        stmt = stmt.order_by(Applicant.assessment_score.desc().nullslast(), Applicant.created_at.desc())
    else:
        stmt = stmt.order_by(Applicant.applied_on.desc(), Applicant.created_at.desc())
    if limit is not None:
        stmt = stmt.limit(limit).offset(offset)
    return list(db.scalars(stmt).all())


def count_applicants(
    db: Session,
    *,
    status: str | None = None,
    search: str | None = None,
    applied_from: date | None = None,
    applied_to: date | None = None,
    rank_by_score: bool = False,
    include_invalid: bool = False,
) -> int:
    """Count applicants using the same filters as ``list_applicants``."""
    stmt = select(func.count()).select_from(Applicant)
    if status is not None:
        if status not in VALID_STATUSES:
            raise BadRequestError(f"Unknown status: {status}")
        stmt = stmt.where(Applicant.status == status)
    if search and (term := search.strip()):
        pattern = f"%{term}%"
        stmt = stmt.where(
            or_(
                Applicant.first_name.ilike(pattern),
                Applicant.last_name.ilike(pattern),
                (Applicant.first_name + " " + Applicant.last_name).ilike(pattern),
            )
        )
    if applied_from is not None:
        stmt = stmt.where(Applicant.applied_on >= applied_from)
    if applied_to is not None:
        stmt = stmt.where(Applicant.applied_on <= applied_to)
    if rank_by_score and not include_invalid:
        stmt = stmt.where(Applicant.assessment_valid.is_not(False))
    return int(db.scalar(stmt) or 0)


def get_applicant(db: Session, *, applicant_id: UUID) -> Applicant:
    applicant = db.get(Applicant, applicant_id)
    if applicant is None:  # missing, or hidden by RLS — same outcome
        raise NotFoundError("Applicant not found.")
    return applicant


def active_intake_choices(db: Session, *, tenant_id: UUID) -> list[dict[str, object]]:
    """Active cohort/track placements an Academy admin may assign."""
    rows = db.execute(
        select(Cohort, Track)
        .join(
            CohortTrack,
            (CohortTrack.tenant_id == Cohort.tenant_id) & (CohortTrack.cohort_id == Cohort.id),
        )
        .join(
            Track,
            (Track.tenant_id == CohortTrack.tenant_id) & (Track.id == CohortTrack.track_id),
        )
        .where(Cohort.tenant_id == tenant_id)
        .where(Cohort.status == "active")
        .where(CohortTrack.status == "active")
        .where(Track.status == "active")
        .order_by(Track.name, Cohort.name)
    ).all()
    return [
        {
            "value": f"{cohort.id}:{track.id}",
            "cohort_id": cohort.id,
            "track_id": track.id,
            "cohort": cohort.name,
            "track": track.name,
        }
        for cohort, track in rows
    ]


def _active_intake_track(
    db: Session,
    *,
    tenant_id: UUID,
    cohort_id: UUID | None,
    track_id: UUID | None,
) -> Track | None:
    if cohort_id is None or track_id is None:
        return None
    return db.scalars(
        select(Track)
        .join(
            CohortTrack,
            (CohortTrack.tenant_id == Track.tenant_id) & (CohortTrack.track_id == Track.id),
        )
        .join(
            Cohort,
            (Cohort.tenant_id == CohortTrack.tenant_id) & (Cohort.id == CohortTrack.cohort_id),
        )
        .where(Track.tenant_id == tenant_id)
        .where(Track.id == track_id)
        .where(Track.status == "active")
        .where(CohortTrack.cohort_id == cohort_id)
        .where(CohortTrack.status == "active")
        .where(Cohort.status == "active")
    ).first()


def has_active_intake(db: Session, *, applicant: Applicant) -> bool:
    return (
        _active_intake_track(
            db,
            tenant_id=applicant.tenant_id,
            cohort_id=applicant.cohort_id,
            track_id=applicant.track_id,
        )
        is not None
    )


def _assign_applicant_intake_locked(db, *, context):
    from app.services.audit_context import _require_human_admission

    state = _require_human_admission(context, db)
    applicant, command = state.applicant, context.resources
    actor_person_id = context.actor_id
    cohort_id, track_id, reason = command.cohort_id, command.track_id, command.reason
    track = _active_intake_track(
        db,
        tenant_id=applicant.tenant_id,
        cohort_id=cohort_id,
        track_id=track_id,
    )
    if track is None:
        raise BadRequestError("That cohort and training track are not active.")
    previous_cohort_id = applicant.cohort_id
    previous_track_id = applicant.track_id
    applicant.cohort_id = cohort_id
    applicant.track_id = track_id
    applicant.program = track.name
    state.expected_binding = (*state.expected_binding[:3], cohort_id, track_id)
    db.flush()
    _write_human_event(
        context,
        db,
        tenant_id=applicant.tenant_id,
        actor_person_id=actor_person_id,
        action="applicant.intake_assigned",
        entity_type="applicant",
        entity_id=str(applicant.id),
        details={
            "from_cohort_id": str(previous_cohort_id) if previous_cohort_id else "",
            "from_track_id": str(previous_track_id) if previous_track_id else "",
            "to_cohort_id": str(cohort_id),
            "to_track_id": str(track_id),
            "track_name": track.name,
            "reason": reason or "",
            "source": "admin_web",
        },
    )
    return applicant


def admin_review_actions(
    applicant: Applicant,
    *,
    placement_ready: bool | None = None,
) -> list[dict[str, object]]:
    """Backend-owned eligibility for the applicant detail action panel."""
    actions: list[str] = []
    if placement_ready is None:
        placement_ready = applicant.cohort_id is not None and applicant.track_id is not None
    if placement_ready and applicant.status in {"applied", "screened", "waitlisted", "accepted"}:
        actions.append("accept")
    if "waitlisted" in ALLOWED_TRANSITIONS.get(applicant.status, frozenset()):
        actions.append("waitlist")
    if "rejected" in ALLOWED_TRANSITIONS.get(applicant.status, frozenset()):
        actions.append("reject")
    if applicant.assessment_taken_at is not None or applicant.assessment_started_at is not None:
        actions.append("reset_assessment")
    elif applicant.assessment_token_hash is not None:
        actions.append("resend_invitation")
        actions.append("extend_access")
    return [
        {
            "key": action,
            "label": ADMIN_ACTION_LABELS[action],
            "destructive": action in {"reject", "reset_assessment"},
        }
        for action in actions
    ]


def applicant_transition_history(db: Session, *, applicant: Applicant) -> list:
    """Authoritative transition/audit events for one applicant, newest first."""
    from app.services.api_identity_reads import applicant_history

    return applicant_history(db, tenant_id=applicant.tenant_id, applicant_id=applicant.id)


def _resend_applicant_invitation_locked(db, *, context):
    try:
        from app.services import entrance_exam
        from app.services.audit_context import _require_human_admission

        state = _require_human_admission(context, db)
        applicant, command = state.applicant, context.resources
        actor_person_id = context.actor_id
        base_url, extend_access, reason = state.base_url, command.extend_access, command.reason
        if applicant.assessment_taken_at is not None:
            raise BadRequestError("This applicant has already completed the assessment.")
        if not entrance_exam.has_entrance_exam(db, applicant=applicant):
            raise BadRequestError("No entrance assessment is configured for this applicant.")

        previous_deadline = applicant.assessment_deadline
        should_extend = extend_access or previous_deadline is None or entrance_exam.past_deadline(applicant)
        state.extended_access = bool(should_extend)
        if should_extend:
            result = entrance_exam.invite(db, applicant=applicant, base_url=base_url)
            state.delivery("entrance_invite", result["emailed"])
        else:
            from urllib.parse import quote

            from app.services import applicant_email

            raw = entrance_exam.issue_token(db, applicant=applicant)
            queued = applicant_email.send_exam_invite(
                db,
                applicant=applicant,
                url=f"{base_url.rstrip('/')}/apply/assessment?token={quote(raw)}",
                minutes=entrance_exam.time_limit_minutes(db, applicant=applicant),
            )
            state.delivery("entrance_invite", queued)
        _write_human_event(
            context,
            db,
            tenant_id=applicant.tenant_id,
            actor_person_id=actor_person_id,
            action="applicant.invitation_resent",
            entity_type="applicant",
            entity_id=str(applicant.id),
            details={
                "reason": reason or "",
                "source": "admin_web",
                "extended_access": bool(should_extend),
                "previous_assessment_deadline": previous_deadline.isoformat() if previous_deadline else "",
                "assessment_deadline": applicant.assessment_deadline.isoformat()
                if applicant.assessment_deadline
                else "",
            },
        )
        return applicant

    finally:
        raw = cast(str, None)
        db = context = state = applicant = command = None


def _extend_applicant_access_locked(db, *, context):
    from app.services.audit_context import _require_human_admission
    from app.services.entrance_invitation_policy import entrance_invitation_policy

    state = _require_human_admission(context, db)
    applicant, command = state.applicant, context.resources
    actor_person_id = context.actor_id
    reason = command.reason
    if applicant.assessment_taken_at is not None:
        raise BadRequestError("This applicant has already completed the assessment.")
    previous = applicant.assessment_deadline
    applicant.assessment_deadline = entrance_invitation_policy(db).deadline(datetime.now(UTC))
    db.flush()
    _write_human_event(
        context,
        db,
        tenant_id=applicant.tenant_id,
        actor_person_id=actor_person_id,
        action="applicant.access_extended",
        entity_type="applicant",
        entity_id=str(applicant.id),
        details={
            "reason": reason or "",
            "source": "admin_web",
            "previous_assessment_deadline": previous.isoformat() if previous else "",
            "assessment_deadline": applicant.assessment_deadline.isoformat() if applicant.assessment_deadline else "",
        },
    )
    return applicant


def _apply_admin_review_action_locked(db, *, context):
    try:
        from app.services.audit_context import _require_human_admission

        state = _require_human_admission(context, db)
        applicant, command = state.applicant, context.resources
        actor_person_id = context.actor_id
        action, reason, base_url = command.action, command.reason, state.base_url
        eligible = {
            str(item["key"])
            for item in admin_review_actions(
                applicant,
                placement_ready=has_active_intake(db, applicant=applicant),
            )
        }
        if action not in eligible:
            raise BadRequestError("That action is not available for the applicant's current state.")

        if action == "resend_invitation":
            return _resend_applicant_invitation_locked(db, context=context)
        if action == "extend_access":
            return _extend_applicant_access_locked(db, context=context)

        if action == "reset_assessment":
            from app.services import entrance_exam

            queued = entrance_exam.reset_and_invite(db, applicant=applicant, base_url=base_url)
            state.delivery("entrance_invite", queued)
            _write_human_event(
                context,
                db,
                tenant_id=applicant.tenant_id,
                actor_person_id=actor_person_id,
                action="applicant.assessment_reset",
                entity_type="applicant",
                entity_id=str(applicant.id),
                details={"reason": reason or "", "source": "admin_web"},
            )
            return applicant

        if action == "reinvite_assessment":
            from app.services import entrance_exam

            result = entrance_exam.invite(db, applicant=applicant, base_url=base_url)
            state.delivery("entrance_invite", result["emailed"])
            _write_human_event(
                context,
                db,
                tenant_id=applicant.tenant_id,
                actor_person_id=actor_person_id,
                action="applicant.assessment_reinvite",
                entity_type="applicant",
                entity_id=str(applicant.id),
                details={"reason": reason or "", "source": "admin_web"},
            )
            return applicant

        if action == "accept":
            path = {
                "applied": ("screened", "accepted", "onboarding"),
                "screened": ("accepted", "onboarding"),
                "waitlisted": ("accepted", "onboarding"),
                "accepted": ("onboarding",),
            }[applicant.status]
            for next_status in path:
                _transition_applicant_locked(
                    db,
                    applicant=applicant,
                    transition_origin=context,
                    to_status=next_status,
                    notes=reason,
                    actor_person_id=actor_person_id,
                    source="admin_web",
                )
            raw = mint_onboarding_token(db, applicant=applicant)
            from app.services import applicant_email

            queued = applicant_email.send_onboarding_invite(
                db,
                applicant=applicant,
                url=f"{base_url.rstrip('/')}/onboarding?token={raw}",
            )
            state.delivery("onboarding_invite", queued)
            return applicant

        target = "waitlisted" if action == "waitlist" else "rejected"
        return _transition_applicant_locked(
            db,
            applicant=applicant,
            transition_origin=context,
            to_status=target,
            notes=reason,
            actor_person_id=actor_person_id,
            source="admin_web",
        )

    finally:
        raw = cast(str, None)
        db = context = state = applicant = command = None


class _LegacyTransitionOrigin:
    """UNQUALIFIED compatibility origin; never authorizes the genuine submit producer."""

    def __init__(self, actor_person_id, source):
        if actor_person_id is not None or source != "assessment_policy":
            raise BadRequestError("human transitions require a genuine Request")
        self.actor_person_id, self.source = actor_person_id, source


def _write_transition_audit(origin, db, **kwargs):
    from app.admission_assessment_context import _AssessmentContext, _write_assessment_event
    from app.services.invitation_issuance import _OnboardingContext

    if type(origin) is _AssessmentContext:
        return _write_assessment_event(origin, db, **kwargs)
    if type(origin) is _LegacyTransitionOrigin:
        if origin.actor_person_id is not None or origin.source != "assessment_policy":
            raise BadRequestError("invalid legacy assessment origin")
        return _legacy_write_audit_event(db, **kwargs)
    from app.services.audit_context import _Context

    if type(origin) is _Context:
        return _write_human_event(origin, db, **kwargs)
    if type(origin) is not _OnboardingContext or origin.db is not db:
        raise BadRequestError("invalid transition origin")
    origin.require()
    origin._check_bearer()
    if (
        kwargs["tenant_id"] != origin.tenant_id
        or kwargs["entity_id"] != str(cast(Applicant, origin.applicant).id)
        or kwargs["action"] != "applicant.transition"
        or kwargs["entity_type"] != "applicant"
        or kwargs["actor_person_id"] is not None
        or cast(Applicant, origin.applicant).status != "enrolled"
        or kwargs["details"]
        != {"from_status": "onboarding", "to_status": "enrolled", "reason": "", "source": "onboarding"}
    ):
        raise BadRequestError("invalid onboarding transition")
    origin._audit(kwargs["action"], kwargs["entity_type"], cast(Applicant, origin.applicant).id, kwargs["details"])


def transition_applicant(
    db: Session,
    *,
    applicant_id: UUID,
    to_status: str,
    notes: str | None = None,
    actor_person_id: UUID | None = None,
    source: str = "admin",
) -> Applicant:
    if to_status not in VALID_STATUSES:
        raise BadRequestError(f"Unknown status: {to_status}")
    applicant = db.scalars(
        select(Applicant)
        .where(Applicant.id == applicant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if applicant is None:
        raise NotFoundError("Applicant not found.")
    origin = _LegacyTransitionOrigin(actor_person_id, source)
    return _transition_applicant_locked(
        db,
        applicant=applicant,
        to_status=to_status,
        notes=notes,
        actor_person_id=actor_person_id,
        source=source,
        transition_origin=origin,
    )


def _transition_onboarding_enrollment(context):
    from app.services.invitation_issuance import _OnboardingContext

    if type(context) is not _OnboardingContext:
        raise BadRequestError("invalid onboarding transition context")
    context.require()
    context._check_bearer()
    return _transition_applicant_locked(
        context.db,
        applicant=context.applicant,
        to_status="enrolled",
        notes=None,
        actor_person_id=None,
        source="onboarding",
        transition_origin=context,
    )


def _transition_applicant_locked(db, *, applicant, to_status, notes, actor_person_id, source, transition_origin):
    if to_status not in VALID_STATUSES:
        raise BadRequestError(f"Unknown status: {to_status}")

    current = applicant.status
    if to_status == current:
        return applicant
    if to_status not in ALLOWED_TRANSITIONS.get(current, frozenset()):
        raise BadRequestError(f"Cannot move applicant from '{current}' to '{to_status}'.")
    if to_status in {"accepted", "onboarding", "enrolled"} and not has_active_intake(
        db,
        applicant=applicant,
    ):
        raise BadRequestError("Assign an active cohort and canonical training track before accepting this applicant.")

    applicant.status = to_status
    if notes:
        applicant.notes = notes
    db.flush()
    _write_transition_audit(
        transition_origin,
        db,
        tenant_id=applicant.tenant_id,
        actor_person_id=actor_person_id,
        action="applicant.transition",
        entity_type="applicant",
        entity_id=str(applicant.id),
        details={
            "from_status": current,
            "to_status": to_status,
            "reason": notes or "",
            "source": source,
        },
    )
    # Entering onboarding seeds the checklist the applicant must clear to enrol.
    if to_status == "onboarding":
        onboarding.seed_tasks(db, tenant_id=applicant.tenant_id, applicant_id=applicant.id)
        # A completed entrance assessment satisfies its onboarding task (carry-forward).
        if applicant.assessment_taken_at is not None:
            onboarding.complete_task_by_key(
                db, tenant_id=applicant.tenant_id, applicant_id=applicant.id, key="entrance_assessment"
            )
    return applicant


def _assessment_policy_transition(db, *, context, **kwargs):
    if context is None:
        return transition_applicant(db, **kwargs)
    from app.admission_assessment_context import _require_assessment

    _require_assessment(context, db, context.applicant)
    if kwargs["applicant_id"] != context.applicant.id or kwargs["source"] != "assessment_policy":
        raise BadRequestError("assessment policy transition differs")
    return _transition_applicant_locked(
        db,
        applicant=context.applicant,
        to_status=kwargs["to_status"],
        notes=kwargs.get("notes"),
        actor_person_id=None,
        source="assessment_policy",
        transition_origin=context,
    )


def apply_assessment_policy(db: Session, *, applicant: Applicant, _assessment_context=None) -> str | None:
    """Decide the pipeline consequence of a graded entrance sitting.

    The single owner of the auto-progression decision. Acts only when the
    applicant is still ``applied``, their cohort sets ``auto_accept_threshold``,
    and the sitting passed the validity gate:

    - score >= threshold → screened → accepted → onboarding (portal token
      minted; the raw is returned so the caller can email the offer).
    - score < threshold → the cohort's ``auto_accept_below_action``:
      ``waitlisted`` (historical default) or untouched for a human.
    - invalid sitting / no cohort / no threshold → untouched: a human decides.

    The decision itself is the shared pure policy in
    :mod:`app.services.admission_assessment_policy`; this function only
    executes its path (transitions, notes, onboarding token).
    """
    if _assessment_context is not None:
        from app.admission_assessment_context import _require_assessment

        _require_assessment(_assessment_context, db, applicant)
    cohort = db.get(Cohort, applicant.cohort_id) if applicant.cohort_id is not None else None
    threshold = cohort.auto_accept_threshold if cohort is not None else None
    below_action = cohort.auto_accept_below_action if cohort is not None else "waitlisted"
    path = assessment_path(
        status=applicant.status,
        score=applicant.assessment_score,
        valid=applicant.assessment_valid is True,
        threshold=threshold,
        below_action=below_action,
    )
    if not path:
        return None

    score = applicant.assessment_score
    assert score is not None and threshold is not None  # assessment_path guarantees both
    if path == ("screened", "accepted", "onboarding"):
        for nxt in path:
            _assessment_policy_transition(
                db,
                applicant_id=applicant.id,
                to_status=nxt,
                source="assessment_policy",
                context=_assessment_context,
            )
        applicant.notes = f"auto-accepted: entrance score {score:.0%} >= threshold {threshold:.0%}"
        db.flush()
        return mint_onboarding_token(db, applicant=applicant)

    _assessment_policy_transition(
        db,
        applicant_id=applicant.id,
        to_status="waitlisted",
        notes=below_threshold_reason(score, threshold),
        source="assessment_policy",
        context=_assessment_context,
    )
    return None


def mint_onboarding_token(db: Session, *, applicant: Applicant) -> str:
    """Mint (or re-mint) the applicant's self-serve onboarding-portal token.

    Returns the raw token — deliver once (email link); only its hash is stored.
    Re-minting invalidates any older link, same as the entrance-exam invite.
    """
    applicant = onboarding._lock_applicant(db, tenant_id=applicant.tenant_id, applicant_id=applicant.id)
    raw = secrets.token_urlsafe(32)
    applicant.onboarding_token_hash = hash_token(raw)
    db.flush()
    return raw


def applicant_for_onboarding_token(db: Session, *, tenant_id: UUID, raw: str) -> Applicant | None:
    """Resolve an onboarding-portal token to its applicant (None if unknown)."""
    if not raw:
        return None
    return db.scalars(
        select(Applicant)
        .where(Applicant.tenant_id == tenant_id)
        .where(Applicant.onboarding_token_hash == hash_token(raw))
    ).first()


def _enroll_locked(db, *, applicant, cohort_id, context=None, actor_person_id=None):
    """Product guards and canonical facts under Party -> Applicant -> Task locks."""
    from uuid import uuid4

    from dotmac_kernel.identity import person_display_name
    from dotmac_kernel.models import PartyType

    from app.services import canonical_identity_store as store
    from app.services.admission_enrollment_policy import admission_enrollment_policy
    from app.services.audit_context import _Context, _require_human_admission
    from app.services.initial_identity_policy import initial_identity_policy
    from app.services.invitation_issuance import _OnboardingContext

    if type(context) is _Context:
        enrollment_context = _require_human_admission(context, db)
    elif type(context) is _OnboardingContext and context.db is db:
        context.require()
        context._check_bearer()
        enrollment_context = context
    else:
        raise BadRequestError("exact human or onboarding enrollment origin required")
    if applicant is not enrollment_context.applicant:
        raise BadRequestError("enrollment applicant binding differs")
    if applicant.status != "onboarding":
        raise BadRequestError(f"Applicant must be in 'onboarding' to enrol (is '{applicant.status}').")
    if not onboarding.is_complete(db, tenant_id=applicant.tenant_id, applicant_id=applicant.id):
        raise BadRequestError("Applicant has outstanding onboarding tasks and cannot enrol yet.")
    if applicant.cohort_id != cohort_id:
        raise BadRequestError("Applicant must be enrolled into their assigned cohort.")
    if not has_active_intake(db, applicant=applicant):
        raise BadRequestError("Applicant has no active canonical training track.")
    person = store.find_party_exact_email(db, tenant_id=applicant.tenant_id, email=applicant.email)
    created = person is None
    if not created:
        person = cast("store.PartyFact", person)
        subtype = store.lock_person(db, tenant_id=applicant.tenant_id, party_id=person.id)
        profile = store.lock_profile(db, tenant_id=applicant.tenant_id, party_id=person.id)
        if person.party_type != PartyType.person or subtype is None or profile is None:
            raise BadRequestError("Admission identity is incomplete.")
    # Required even for enrollment reuse; compose before any identity/enrollment DML.
    initial_state = admission_enrollment_policy(db)
    if created:
        policy = initial_identity_policy(db)
        now = datetime.now(UTC)
        person = store.create_person(
            db,
            values=store.NewPersonValues(
                id=uuid4(),
                tenant_id=applicant.tenant_id,
                email=applicant.email,
                display_name=person_display_name(applicant.first_name, applicant.last_name),
                first_name=applicant.first_name,
                last_name=applicant.last_name,
                is_active=policy.is_active,
                custom_fields={},
                profile_status=policy.profile_status,
                profile_avatar_path=None,
                profile_prefs={},
                created_at=now,
                updated_at=now,
            ),
        )
    person = cast("store.PartyFact", person)
    subtype = store.lock_person(db, tenant_id=applicant.tenant_id, party_id=person.id)
    profile = store.lock_profile(db, tenant_id=applicant.tenant_id, party_id=person.id)
    if person.party_type != PartyType.person or subtype is None or profile is None:
        raise BadRequestError("Admission identity is incomplete.")
    person = store.lock_parties(db, tenant_id=applicant.tenant_id, party_ids=(person.id,))[0]
    if created and (person.is_active is not policy.is_active or profile.status != policy.profile_status):
        raise BadRequestError("Admission initial identity state differs.")
    enrollment_context.target_id = person.id
    enrollment_context.identity = enrollment_context._identity_state()
    enrollment_context.original_roles = enrollment_context._roles()
    if type(context) is _Context:
        enrollment_context.credentials_before = enrollment_context._credentials()
    learning.ensure_admission_enrollment(
        db,
        learning.EnsureAdmissionEnrollmentCommand(
            tenant_id=applicant.tenant_id, cohort_id=cohort_id, person_id=person.id, track_id=applicant.track_id
        ),
        initial_state=initial_state,
    )
    if (
        enrollment_context._identity_state() != enrollment_context.identity
        or enrollment_context._roles() != enrollment_context.original_roles
    ):
        raise BadRequestError("Admission identity changed during enrollment.")
    if type(context) is _Context and enrollment_context._credentials() != enrollment_context.credentials_before:
        raise BadRequestError("Admission credentials changed during enrollment.")
    applicant.person_id = person.id
    db.flush()
    if type(context) is _Context:
        enrollment_context.expected_binding = (
            applicant.email,
            person.id,
            person.id,
            applicant.cohort_id,
            applicant.track_id,
        )
        return _transition_applicant_locked(
            db,
            applicant=applicant,
            to_status="enrolled",
            notes=None,
            actor_person_id=context.actor_id,
            source="admin_api",
            transition_origin=context,
        )
    context.linked_id = context.identified_id = person.id
    return _transition_onboarding_enrollment(context)


def enroll_applicant(db: Session, *, request, applicant_id: UUID, cohort_id: UUID) -> Applicant:
    try:
        return enroll_api_applicant(db, request=request, applicant_id=applicant_id, cohort_id=cohort_id)

    finally:
        db = cast(Session, None)
        request = None


def _write_human_event(context, db, **kwargs):
    from app.services.audit_context import _require_human_admission

    state = _require_human_admission(context, db)
    if (
        kwargs["tenant_id"] != context.tenant_id
        or kwargs["actor_person_id"] != context.actor_id
        or kwargs["entity_type"] != "applicant"
        or kwargs["entity_id"] != str(context.resources.entity_id)
    ):
        raise BadRequestError("human admission audit resource differs")
    state.issue(kwargs["action"], kwargs["details"])


def _execute_human_admission(context):
    try:
        from app.services import audit_context as boundary

        state = boundary._require_human_admission(context, context.db)
        db, applicant, command = context.db, state.applicant, context.resources
        if context.operation is boundary._ADMISSIONS_INTAKE:
            return _assign_applicant_intake_locked(db, context=context)
        if context.operation is boundary._ADMISSIONS_REVIEW:
            # Preserve the WEB special resend+extend bypass of the action panel gate.
            if command.action == "resend_invitation" and command.extend_access:
                return _resend_applicant_invitation_locked(db, context=context)
            return _apply_admin_review_action_locked(db, context=context)
        if context.operation is boundary._ADMISSIONS_ENROLL:
            return _enroll_locked(db, applicant=applicant, cohort_id=command.cohort_id, context=context)
        if context.operation is not boundary._ADMISSIONS_TRANSITION:
            raise BadRequestError("unsupported human admission command")
        result = _transition_applicant_locked(
            db,
            applicant=applicant,
            to_status=command.action,
            notes=command.reason,
            actor_person_id=context.actor_id,
            source="admin_api",
            transition_origin=context,
        )
        if command.action == "onboarding":
            from app.services import applicant_email

            raw = mint_onboarding_token(db, applicant=applicant)
            queued = applicant_email.send_onboarding_invite(
                db, applicant=applicant, url=f"{state.base_url}/onboarding?token={raw}"
            )
            state.delivery("onboarding_invite", queued)
        return result

    finally:
        raw = cast(str, None)
        db = context = state = applicant = command = None


def assign_web_applicant_intake(db, *, request, applicant_id, cohort_id, track_id, reason=None):
    try:
        from app.services import audit_context as boundary

        with boundary.assign_web_applicant_intake(
            db, request=request, applicant_id=applicant_id, cohort_id=cohort_id, track_id=track_id, reason=reason
        ) as context:
            return context.execute_admission()

    finally:
        db = request = None


def review_web_applicant(db, *, request, applicant_id, action, reason=None, extend_access=False):
    try:
        from app.services import audit_context as boundary

        with boundary.review_web_applicant(
            db, request=request, applicant_id=applicant_id, action=action, reason=reason, extend_access=extend_access
        ) as context:
            return context.execute_admission()

    finally:
        db = request = None


def transition_api_applicant(db, *, request, applicant_id, to_status, notes=None):
    try:
        from app.services import audit_context as boundary

        with boundary.transition_api_applicant(
            db, request=request, applicant_id=applicant_id, to_status=to_status, notes=notes
        ) as context:
            return context.execute_admission()

    finally:
        db = request = None


def enroll_api_applicant(db, *, request, applicant_id, cohort_id):
    try:
        from app.services import audit_context as boundary

        with boundary.enroll_api_applicant(
            db, request=request, applicant_id=applicant_id, cohort_id=cohort_id
        ) as context:
            return context.execute_admission()

    finally:
        db = request = None


def assign_applicant_intake(db, *, request, applicant_id, cohort_id, track_id, reason=None):
    try:
        return assign_web_applicant_intake(
            db, request=request, applicant_id=applicant_id, cohort_id=cohort_id, track_id=track_id, reason=reason
        )

    finally:
        db = request = None


def apply_admin_review_action(db, *, request, applicant_id, action, reason=None):
    try:
        return review_web_applicant(db, request=request, applicant_id=applicant_id, action=action, reason=reason)

    finally:
        db = request = None


def resend_applicant_invitation(db, *, request, applicant_id, extend_access=False, reason=None):
    try:
        return review_web_applicant(
            db,
            request=request,
            applicant_id=applicant_id,
            action="resend_invitation",
            reason=reason,
            extend_access=extend_access,
        )

    finally:
        db = request = None


def extend_applicant_access(db, *, request, applicant_id, reason=None):
    try:
        return review_web_applicant(
            db, request=request, applicant_id=applicant_id, action="extend_access", reason=reason
        )

    finally:
        db = request = None
