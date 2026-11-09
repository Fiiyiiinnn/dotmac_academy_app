"""Scheduling service — cohort timetables of class sessions.

Public scheduling operations delegate to Learning and return detached facts.
Updates require the genuine request-bound human operation boundary.

Repo convention: ``db`` + explicit ids, ``flush`` not ``commit``, domain
exceptions for the router to translate. RLS scopes reads to the tenant.
"""

# Ported at new-composition genesis (presentation-coupled wave).

from __future__ import annotations

from datetime import datetime
from typing import cast
from uuid import UUID

import dotmac_learning as learning
from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from app.models.class_session import SESSION_TYPES as SESSION_TYPES
from app.services.audit_context import update_web_session
from app.services.exceptions import BadRequestError, NotFoundError
from app.services.learning_policy import creation_defaults
from app.services.settings_store import effective

DELIVERY_MODES = frozenset({"self_paced", "live", "blended"})

_UNSET = object()  # sentinel: "field not provided" vs. None ("clear this field")


def set_delivery_mode(db: Session, *, tenant_id: UUID, cohort_id: UUID, mode: str) -> learning.CohortDeliveryFact:
    try:
        return learning.set_delivery_mode(
            db, learning.SetDeliveryModeCommand(tenant_id=tenant_id, cohort_id=cohort_id, mode=mode)
        )
    except learning.SchedulingBadRequest as exc:
        raise BadRequestError(str(exc)) from exc
    except learning.SchedulingNotFound as exc:
        raise NotFoundError(str(exc)) from exc


def create_session(
    db: Session,
    *,
    tenant_id: UUID,
    cohort_id: UUID,
    title: str,
    starts_at: datetime,
    session_type: str | None = None,
    ends_at: datetime | None = None,
    offering_id: UUID | None = None,
    instructor_person_id: UUID | None = None,
    location: str | None = None,
    join_url: str | None = None,
    notes: str | None = None,
) -> learning.ClassSessionFact:
    if session_type is None:
        session_type = cast(str, effective(db, require_store=True)["learning_default_session_type"])
    try:
        return learning.create_session(
            db,
            learning.CreateSessionCommand(
                tenant_id=tenant_id,
                cohort_id=cohort_id,
                title=title,
                starts_at=starts_at,
                session_type=session_type,
                promote_delivery_mode=creation_defaults(db).session_promotes_delivery_mode,
                ends_at=ends_at,
                offering_id=offering_id,
                instructor_person_id=instructor_person_id,
                location=location,
                join_url=join_url,
                notes=notes,
            ),
        )
    except learning.SchedulingBadRequest as exc:
        raise BadRequestError(str(exc)) from exc
    except learning.SchedulingNotFound as exc:
        raise NotFoundError(str(exc)) from exc


def get_session(db: Session, *, tenant_id: UUID, session_id: UUID) -> learning.ClassSessionFact:
    """Detached owner fact for rendering; not a mutable Academy ORM alias."""
    try:
        return learning.get_session(db, tenant_id=tenant_id, session_id=session_id)
    except learning.SchedulingBadRequest as exc:
        raise BadRequestError(str(exc)) from exc
    except learning.SchedulingNotFound as exc:
        raise NotFoundError(str(exc)) from exc


def list_for_cohort(
    db: Session, *, tenant_id: UUID, cohort_id: UUID, upcoming_only: bool = False, now: datetime | None = None
) -> list[learning.ClassSessionFact]:
    """The cohort's timetable, in owner order, with detached fact leaves."""
    try:
        return list(
            learning.list_cohort_sessions(
                db, tenant_id=tenant_id, cohort_id=cohort_id, upcoming_only=upcoming_only, now=now
            )
        )
    except learning.SchedulingBadRequest as exc:
        raise BadRequestError(str(exc)) from exc
    except learning.SchedulingNotFound as exc:
        raise NotFoundError(str(exc)) from exc


def update_session(
    db: Session,
    *,
    request: Request,
    tenant_id: UUID,
    session_id: UUID,
    title=_UNSET,
    starts_at=_UNSET,
    ends_at=_UNSET,
    session_type=_UNSET,
    location=_UNSET,
    join_url=_UNSET,
    notes=_UNSET,
    status=_UNSET,
) -> tuple[learning.ClassSessionFact, dict[str, dict[str, object]]]:
    """Edit through the genuine Learning owner, preserving omitted versus None."""
    human = result = supplied = None
    try:
        supplied = tuple(
            (key, value)
            for key, value in (
                ("title", title),
                ("starts_at", starts_at),
                ("ends_at", ends_at),
                ("session_type", session_type),
                ("location", location),
                ("join_url", join_url),
                ("notes", notes),
                ("status", status),
            )
            if value is not _UNSET
        )
        with update_web_session(db, request=request, session_id=session_id, supplied=supplied) as human:
            if human.tenant_id != tenant_id:
                raise HTTPException(status_code=403, detail="Forbidden")
            result = human.update_session()
            return result.session, {
                change.field: {"from": change.from_value, "to": change.to_value} for change in result.changes
            }
    finally:
        db = cast(Session, None)
        request = cast(Request, None)
        human = result = supplied = title = starts_at = ends_at = session_type = None
        location = join_url = notes = status = None
        tenant_id = cast(UUID, None)
        session_id = cast(UUID, None)


def cancel_session(db: Session, *, tenant_id: UUID, session_id: UUID) -> learning.ClassSessionFact:
    try:
        return learning.cancel_session(db, learning.CancelSessionCommand(tenant_id=tenant_id, session_id=session_id))
    except learning.SchedulingBadRequest as exc:
        raise BadRequestError(str(exc)) from exc
    except learning.SchedulingNotFound as exc:
        raise NotFoundError(str(exc)) from exc


def list_for_person(
    db: Session, *, tenant_id: UUID, person_id: UUID, now: datetime
) -> dict[str, list[tuple[learning.ClassSessionFact, str]]]:
    timetable = learning.person_timetable(db, tenant_id=tenant_id, person_id=person_id, now=now)
    return {
        "upcoming": [(entry.session, entry.cohort_name) for entry in timetable.upcoming],
        "past": [(entry.session, entry.cohort_name) for entry in timetable.past],
    }


def get_session_for_person(
    db: Session, *, tenant_id: UUID, person_id: UUID, session_id: UUID
) -> tuple[learning.ClassSessionFact, str]:
    try:
        entry = learning.get_session_for_person(db, tenant_id=tenant_id, person_id=person_id, session_id=session_id)
    except learning.SchedulingNotFound as exc:
        raise NotFoundError(str(exc)) from exc
    return entry.session, entry.cohort_name


def join_is_open(session: learning.ClassSessionFact, *, now: datetime, lead_minutes: int) -> bool:
    """Use the caller's resolved join lead; the owner applies the join formula."""
    return learning.join_is_open(session, now=now, lead_minutes=lead_minutes)
