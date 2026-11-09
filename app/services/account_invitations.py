"""Canonical invitation/role/enrollment decisions, under private human authority.

Nonhuman legacy callers must port their genuine provenance before adoption.
No actor boolean, caller ORM object or tenant UUID alone reaches this writer.
"""

# Ported at new-composition genesis (admissions services slice).

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

import dotmac_learning as learning

from app.services.exceptions import BadRequestError, ConflictError, NotFoundError
from app.services.identity import normalize_email
from app.services.invitation_policy import invitation_enrollment_policy
from app.services.learning_policy import creation_defaults


@dataclass(frozen=True, slots=True)
class CohortAssignment:
    cohort_id: UUID
    course_ids: tuple[UUID, ...] = ()
    track_id: UUID | None = None

    def __post_init__(self):
        if (
            type(self.cohort_id) is not UUID
            or type(self.course_ids) is not tuple
            or any(type(value) is not UUID for value in self.course_ids)
            or (self.track_id is not None and type(self.track_id) is not UUID)
        ):
            raise ValueError("cohort assignment requires UUID IDs")


@dataclass(frozen=True, slots=True)
class InvitationAccountView:
    id: UUID
    tenant_id: UUID
    email: str | None
    first_name: str
    last_name: str
    status: str
    is_active: bool


@dataclass(frozen=True, slots=True)
class AccountInvitationResult:
    person: InvitationAccountView
    token: str | None = field(repr=False)
    created_person: bool
    had_credential: bool
    _assignments: tuple[str, ...] = ()

    @property
    def assignments(self) -> list[str]:
        return list(self._assignments)


def _apply_assignment(context, *, role, assignment, role_policy, pacing_default_open):
    """Learning owns persisted selection/enrollment; product owns descriptions."""
    context.require_assignment(role, assignment)
    cohort, courses, track = context.assignment_facts(assignment)
    try:
        result = learning.ensure_invitation_enrollment(
            context.db,
            learning.EnsureInvitationEnrollmentCommand(
                tenant_id=context.tenant_id,
                cohort_id=cohort.id,
                person_id=context.target.id,
                global_role=role,
                pacing_default_open=pacing_default_open,
                track_id=track.id if track is not None else None,
                selected_course_ids=tuple(course.id for course in courses),
            ),
            authority=learning.RosterAuthority(tenant_id=context.tenant_id, actor_is_admin=context.actor_is_admin),
            role_policy=role_policy,
            initial_state=role_policy.initial_state,
        )
    except learning.ParticipationNotFound as exc:
        raise NotFoundError(str(exc)) from exc
    except (learning.ParticipationBadRequest, learning.ParticipationForbidden) as exc:
        raise BadRequestError(str(exc)) from exc
    activation = result.activation
    note = ""
    if (
        activation is not None
        and activation.outcome == "reactivated"
        and activation.previous_status in ("dropped", "waitlisted")
    ):
        note = f" — reactivated (was {activation.previous_status})"
    if result.preserved_instructor_role:
        descriptions = [f"{cohort.name} cohort — already an instructor, role unchanged{note}"]
    else:
        descriptions = [f"{cohort.name} cohort as {result.member_role}{note}"]
    if track is not None:
        descriptions.append(f"{track.name} track in {cohort.name}")
    descriptions.extend(f"{course.title} course for {cohort.name}" for course in courses)
    return descriptions


def _invite_locked(context, command):
    context.require(command)
    # Resolve the enrollment policy before any identity/role/token mutation.
    role_policy = invitation_enrollment_policy(context.db) if command.assignments else None
    pacing_default_open = creation_defaults(context.db).pacing_default_open if command.assignments else None
    had_credential = bool(context.credentials)
    if command.purpose == "strict" and had_credential:
        raise ConflictError("account already has a credential")
    if command.purpose == "resend" and had_credential:
        return AccountInvitationResult(context.view(), None, False, True)
    created = context.target is None
    if created:
        context.create_identity(command.first_name, command.last_name, normalize_email(command.email))
    elif command.purpose != "resend":
        context.synchronize_aliases()
    if command.purpose != "resend":
        context.ensure_role(command.role)
    descriptions = []
    for assignment in command.assignments:
        descriptions.extend(
            _apply_assignment(
                context,
                role=command.role,
                assignment=assignment,
                role_policy=role_policy,
                pacing_default_open=pacing_default_open,
            )
        )
    context.db.flush()
    context.revalidate()
    token = None if had_credential else context.issue_token(ttl_hours=command.ttl_hours)
    return AccountInvitationResult(context.view(), token, created, had_credential, tuple(descriptions))


def invite_and_enroll(db, *, request, email, first_name, last_name, role, assignments=(), ttl_hours=None):
    """Strict generic human context; existing custom global roles remain valid.

    Returns an operation context manager so delivery enqueue belongs to the same
    public savepoint. Old ORM/actor_is_admin/tenant-only callers fail closed and
    remain explicit nonhuman/lifecycle integration gates.
    """
    from app.services.invitation_issuance import _human_operation

    return _human_operation(
        db,
        request=request,
        purpose="generic",
        email=email,
        first_name=first_name,
        last_name=last_name,
        role=role,
        assignments=tuple(assignments),
        ttl_hours=ttl_hours,
    )
