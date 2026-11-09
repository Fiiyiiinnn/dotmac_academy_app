# Ported at new-composition genesis (learning-facing wave).
# app/services/tracks.py
"""Thin product facades over Learning's caller-session track owners.

Tracks narrow the existing cohort/course availability boundary. The read
facades retain dictionary/list/set containers over immutable public facts;
writer results are immutable facts rather than ORM aliases or live refreshes.
The legacy None-name convenience remains an empty name for domain validation.

Mutation owners require an already active caller root transaction and preserve
its root/savepoint boundaries. Authentication, account policy and transaction
creation remain with their existing product and Kernel owners.
"""

from __future__ import annotations

from uuid import UUID

import dotmac_learning as learning
from sqlalchemy.orm import Session

from app.services.exceptions import BadRequestError, NotFoundError
from app.services.learning_policy import creation_defaults


def list_cohort_tracks(db: Session, *, tenant_id: UUID, cohort_id: UUID) -> list[dict]:
    """Keep UI containers over immutable Learning facts on the caller's session."""
    try:
        items = learning.list_cohort_tracks(db, tenant_id=tenant_id, cohort_id=cohort_id)
    except learning.ParticipationNotFound as exc:
        raise NotFoundError(str(exc)) from exc
    return [
        {
            "cohort_track": item.cohort_track,
            "track": item.track,
            "courses": list(item.courses),
            "zero_activity_course_ids": set(item.zero_activity_course_ids),
        }
        for item in items
    ]


def tracks_for_cohorts(db: Session, *, tenant_id: UUID, cohort_ids: list[UUID]) -> dict[UUID, list[learning.TrackFact]]:
    """Adapt Learning's grouped value tuples to the product's list containers."""
    grouped = learning.tracks_for_cohorts(db, tenant_id=tenant_id, cohort_ids=cohort_ids)
    return {cohort_id: list(tracks) for cohort_id, tracks in grouped.items()}


def create_cohort_track(
    db: Session,
    *,
    tenant_id: UUID,
    cohort_id: UUID,
    name: str,
    course_ids: list[UUID],
    positions: dict[UUID, int] | None = None,
) -> learning.TrackFact:
    """Create through Learning; return an immutable value projection of the track."""
    defaults = creation_defaults(db)
    command = learning.CreateTrackCommand(
        tenant_id=tenant_id,
        cohort_id=cohort_id,
        name=name or "",
        course_ids=tuple(course_ids),
        gating_mode=defaults.track_default_gating_mode,
        pacing_default_open=defaults.pacing_default_open,
        positions=positions or {},
    )
    try:
        return learning.create_cohort_track(db, command)
    except learning.ParticipationBadRequest as exc:
        raise BadRequestError(str(exc)) from exc
    except learning.ParticipationNotFound as exc:
        raise NotFoundError(str(exc)) from exc


def add_courses_to_cohort_track(
    db: Session,
    *,
    tenant_id: UUID,
    cohort_id: UUID,
    track_id: UUID,
    course_ids: list[UUID],
    positions: dict[UUID, int] | None = None,
) -> learning.TrackFact:
    """Append courses through Learning using the caller's session and order hints."""
    command = learning.AddTrackCoursesCommand(
        tenant_id=tenant_id,
        cohort_id=cohort_id,
        track_id=track_id,
        course_ids=tuple(course_ids),
        pacing_default_open=creation_defaults(db).pacing_default_open,
        positions=positions or {},
    )
    try:
        return learning.add_courses_to_cohort_track(db, command)
    except learning.ParticipationBadRequest as exc:
        raise BadRequestError(str(exc)) from exc
    except learning.ParticipationNotFound as exc:
        raise NotFoundError(str(exc)) from exc


def reorder_track_courses(
    db: Session,
    *,
    tenant_id: UUID,
    track_id: UUID,
    ordered_course_ids: list[UUID],
) -> learning.TrackFact:
    """Delegate exact membership validation and dense ordering to Learning."""
    command = learning.ReorderTrackCoursesCommand(
        tenant_id=tenant_id,
        track_id=track_id,
        ordered_course_ids=tuple(ordered_course_ids),
    )
    try:
        return learning.reorder_track_courses(db, command)
    except learning.ParticipationBadRequest as exc:
        raise BadRequestError(str(exc)) from exc
    except learning.ParticipationNotFound as exc:
        raise NotFoundError(str(exc)) from exc


def cohort_track_or_404(db: Session, *, tenant_id: UUID, cohort_id: UUID, track_id: UUID) -> learning.CohortTrackFact:
    """Read the active cohort/track link through Learning's tenant-scoped owner."""
    try:
        return learning.cohort_track_or_404(db, tenant_id=tenant_id, cohort_id=cohort_id, track_id=track_id)
    except learning.ParticipationNotFound as exc:
        raise NotFoundError(str(exc)) from exc


def _require_caller_root(db: Session) -> None:
    """Refuse before the settings read can autobegin a root the caller never opened.

    Learning repeats this check authoritatively; it must simply run first.
    """
    root = db.get_transaction()
    if root is None or not root.is_active:
        raise learning.TransactionChanged("An already active caller root transaction is required.")


def ensure_track_offerings(db: Session, *, tenant_id: UUID, cohort_id: UUID, track_id: UUID) -> None:
    """Delegate offering creation/reactivation on the existing caller transaction."""
    _require_caller_root(db)
    try:
        return learning.ensure_track_offerings(
            db,
            tenant_id=tenant_id,
            cohort_id=cohort_id,
            track_id=track_id,
            pacing_default_open=creation_defaults(db).pacing_default_open,
        )
    except learning.ParticipationNotFound as exc:
        raise NotFoundError(str(exc)) from exc


def assign_enrollment_track(
    db: Session,
    *,
    tenant_id: UUID,
    cohort_id: UUID,
    person_id: UUID,
    track_id: UUID,
) -> learning.EnrollmentFact:
    """Preserve Learning's track/offering/enrollment validation and flush order."""
    _require_caller_root(db)
    try:
        return learning.assign_enrollment_track(
            db,
            tenant_id=tenant_id,
            cohort_id=cohort_id,
            person_id=person_id,
            track_id=track_id,
            pacing_default_open=creation_defaults(db).pacing_default_open,
        )
    except learning.ParticipationNotFound as exc:
        raise NotFoundError(str(exc)) from exc


def clear_enrollment_track(
    db: Session,
    *,
    tenant_id: UUID,
    cohort_id: UUID,
    person_id: UUID,
) -> learning.EnrollmentFact:
    """Clear a stuck track through Learning without fabricating completion facts."""
    try:
        return learning.clear_enrollment_track(db, tenant_id=tenant_id, cohort_id=cohort_id, person_id=person_id)
    except learning.ParticipationNotFound as exc:
        raise NotFoundError(str(exc)) from exc
