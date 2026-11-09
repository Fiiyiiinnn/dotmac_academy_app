"""Prepared private human operation boundary; no routes are wired here.

Factories fix transport, role gate and operation. UUIDs and emails identify
resources only. A yielded instance is usable only inside its issuing boundary.
Roster factories deliberately have no audit action: roster has no such event.
"""

# Ported at new-composition genesis (presentation-coupled wave).

from __future__ import annotations

import hmac
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from html import escape
from typing import TYPE_CHECKING, cast
from uuid import UUID, uuid4

import dotmac_learning as learning
from dotmac_kernel.audit import AuditEvent, write_audit_event
from dotmac_kernel.identity import person_display_name
from dotmac_kernel.models import AuthSession, Party, PartyPerson, PartyRoleGrant, PartyType, Role, UserCredential
from dotmac_kernel.transactions import conflict_savepoint, conflict_savepoint_requires_rollback
from dotmac_learning import TransactionChanged
from dotmac_learning.delivery_models import ClassSession, SuccessQueueEntry
from dotmac_learning.participation_models import Cohort, CohortTrack, Enrollment, Track
from fastapi import HTTPException, Request
from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import InstrumentedAttribute, Session
from starlette.datastructures import Headers
from starlette.requests import cookie_parser

from app.api.deps import require_tenant
from app.models.admissions import Applicant
from app.models.email_outbox import EmailOutbox
from app.models.identity_extensions import AcademyAccountProfile, AcademyCredentialPolicy
from app.models.notification import Notification
from app.services import admissions, notifications, onboarding
from app.services import canonical_identity_store as store
from app.services.api_identity_reads import ApiPersonView, get_person
from app.services.email_outbox import enqueue_email
from app.services.exceptions import BadRequestError, ConflictError, NotFoundError
from app.services.identity import normalize_email
from app.services.initial_identity_policy import initial_identity_policy
from app.services.learning_policy import success_terms
from app.services.security import decode_access_token, hash_token
from app.services.web_auth import COOKIE

if TYPE_CHECKING:
    _PersistedValues = tuple[tuple[str, object], ...]
    _AuditReceipt = tuple[AuditEvent, UUID, _PersistedValues]
    _HumanAuditReceipt = tuple[AuditEvent, UUID, dict[str, object], datetime | None]
    _SchedulingOutcome = tuple[
        learning.SessionUpdate,
        tuple[object, ...],
        tuple[tuple[str, str | None, str | None], ...],
        UUID | None,
        _AuditReceipt | None,
    ]
    _SuccessOutcome = tuple[
        learning.SuccessTransitionEffect | learning.SegmentMessageEffect, tuple[object, ...], UUID, _AuditReceipt
    ]
    _CreatedGrant = tuple[PartyRoleGrant | None, tuple[UUID, UUID, UUID, UUID, datetime, datetime]]
    _CreatedPerson = tuple[store.NewPersonValues, Party, PartyPerson, AcademyAccountProfile, ApiPersonView]
    _CreatedRole = tuple[Role, tuple[UUID, UUID, str, str, datetime, datetime]]
    _DeletedPerson = tuple[UUID, datetime, _HumanAuditReceipt]
    _IdentityModel = (
        type[Party]
        | type[PartyPerson]
        | type[AcademyAccountProfile]
        | type[AuthSession]
        | type[PartyRoleGrant]
        | type[UserCredential]
    )
    _SuccessDeliveryRecord = tuple[
        UUID, str, Notification, UUID, _PersistedValues, EmailOutbox | None, UUID | None, _PersistedValues | None
    ]


class HumanOperationError(Exception):
    """Operation failed; messages never contain token/provider material."""


class _Denied(Exception):
    def __init__(self, role=False):
        self.role = role


@dataclass(frozen=True, slots=True)
class _Operation:
    name: str
    transport: str
    roles: frozenset[str]
    action: str | None = None
    entity_type: str | None = None


_PERSON_CREATE = _Operation("api.person_create", "api", frozenset({"admin"}), "person.create", "person")
_PERSON_DELETE = _Operation("api.person_delete", "api", frozenset({"admin"}), "person.delete", "person")
_ROLE_CREATE = _Operation("api.role_create", "api", frozenset({"admin"}), "role.create", "role")
_ROLE_GRANT = _Operation("api.role_grant", "api", frozenset({"admin"}), "role.grant", "person_role")
_SCHEDULING_UPDATE = _Operation(
    "web.update_session", "web", frozenset({"admin", "instructor"}), "scheduling.update_session", "class_session"
)
_ROSTER_STATE = _Operation("web.set_roster_state", "web", frozenset({"admin", "instructor"}))
_REMOVE_FACILITATOR = _Operation("web.remove_facilitator", "web", frozenset({"admin"}))
_FACILITATOR = _Operation("web.assign_facilitator", "web", frozenset({"admin", "instructor"}))
_BULK_ENROLL = _Operation("web.bulk_enroll", "web", frozenset({"admin", "instructor"}))
_RESEND_ACCOUNT = _Operation("web.resend_account_invitation", "web", frozenset({"admin"}))


_SUCCESS_ACKNOWLEDGE = _Operation(
    "web.success_acknowledge",
    "web",
    frozenset({"admin", "instructor"}),
    "success_queue.acknowledge",
    "success_queue_entry",
)
_SUCCESS_ASSIGN = _Operation(
    "web.success_assign", "web", frozenset({"admin", "instructor"}), "success_queue.assign", "success_queue_entry"
)
_SUCCESS_RESOLVE = _Operation(
    "web.success_resolve", "web", frozenset({"admin", "instructor"}), "success_queue.resolve", "success_queue_entry"
)
_SUCCESS_MESSAGE = _Operation(
    "web.success_message", "web", frozenset({"admin", "instructor"}), "success_queue.message_segment", "cohort"
)
_SUCCESS_TRANSITIONS = (_SUCCESS_ACKNOWLEDGE, _SUCCESS_ASSIGN, _SUCCESS_RESOLVE)
_SUCCESS_OPERATIONS = (*_SUCCESS_TRANSITIONS, _SUCCESS_MESSAGE)
_LEARNING_OPERATIONS = (_SCHEDULING_UPDATE, *_SUCCESS_OPERATIONS)


@dataclass(frozen=True, slots=True)
class _SuccessResources:
    entity_id: UUID
    action: str = ""
    segment: str = ""
    subject: str = ""
    body: str = ""
    now: datetime | None = None
    party_id: None = None
    emails: tuple = ()


def _success_values(resources):
    try:
        if (
            type(resources.entity_id) is not UUID
            or any(
                type(value) is not str
                for value in (resources.action, resources.segment, resources.subject, resources.body)
            )
            or (resources.now is not None and type(resources.now) is not datetime)
            or resources.party_id is not None
            or resources.emails != ()
        ):
            raise HumanOperationError("success command resource binding differs")
        return (
            str(resources.entity_id),
            resources.action,
            resources.segment,
            resources.subject,
            resources.body,
            resources.now.isoformat() if resources.now is not None else None,
        )
    finally:
        resources = None


def _persisted_values(row):
    return tuple((column.key, deepcopy(getattr(row, column.key))) for column in row.__table__.columns)


class _SuccessDelivery:
    """Fixed product delivery owned by the issuing WEB operation."""

    def __init__(self, context):
        self._context = context

    def __call__(self, db, person, message, action_id):
        html: str | None
        created: bool | None
        context = self._context
        notification = outbox = record = None
        try:
            context.require()
            r = context.resources
            if (
                context.operation is not _SUCCESS_MESSAGE
                or not context._success_started
                or context._success_outcome is not None
                or context._success_delivery is not self
                or db is not context.db
                or type(person) is not learning.SuccessPersonFact
                or type(message) is not learning.SegmentMessageFact
                or person.tenant_id != context.tenant_id
                or (
                    message.tenant_id,
                    message.cohort_id,
                    message.segment,
                    message.subject,
                    message.body,
                    message.notification_kind,
                    message.email_kind,
                    message.idempotency_prefix,
                )
                != (
                    context.tenant_id,
                    r.entity_id,
                    r.segment,
                    r.subject,
                    r.body,
                    "instructor_message",
                    "segment_message",
                    "segmsg",
                )
                or type(action_id) is not str
                or len(action_id) != 12
                or any(char not in "0123456789abcdef" for char in action_id)
                or any(item[0] == person.id for item in context._success_deliveries)
                or (context._success_deliveries and context._success_deliveries[0][1] != action_id)
            ):
                raise HumanOperationError("success delivery binding differs")
            notification = notifications.notify(
                db,
                tenant_id=context.tenant_id,
                person_id=person.id,
                kind="instructor_message",
                title=r.subject,
                body=r.body,
                link="/",
            )
            db.refresh(notification)
            if type(notification) is not Notification or (
                notification.tenant_id,
                notification.person_id,
                notification.kind,
                notification.title,
                notification.body,
                notification.link,
            ) != (context.tenant_id, person.id, "instructor_message", r.subject, r.body, "/"):
                raise HumanOperationError("success notification differs")
            key = f"segmsg:{action_id}:{person.id}"
            html = "<p>" + escape(r.body).replace("\n", "<br>") + "</p>"
            created = enqueue_email(
                db,
                tenant_id=context.tenant_id,
                idempotency_key=key,
                kind="segment_message",
                recipient=person.email,
                subject=r.subject,
                html_body=html,
                text_body=r.body,
                payload={"segment": r.segment, "cohort_id": str(r.entity_id)},
            )
            if type(created) is not bool:
                raise HumanOperationError("success outbox result differs")
            outbox = db.scalars(
                select(EmailOutbox)
                .where(EmailOutbox.tenant_id == context.tenant_id, EmailOutbox.idempotency_key == key)
                .execution_options(populate_existing=True)
            ).first()
            if person.email:
                if outbox is None or (
                    outbox.kind,
                    outbox.recipient,
                    outbox.subject,
                    outbox.html_body,
                    outbox.text_body,
                    outbox.payload,
                ) != (
                    "segment_message",
                    person.email,
                    r.subject,
                    html,
                    r.body,
                    {"segment": r.segment, "cohort_id": str(r.entity_id)},
                ):
                    raise HumanOperationError("success outbox consequence differs")
            elif created or outbox is not None:
                raise HumanOperationError("missing-email success outbox differs")
            record = (
                person.id,
                action_id,
                notification,
                notification.id,
                _persisted_values(notification),
                outbox,
                outbox.id if outbox is not None else None,
                _persisted_values(outbox) if outbox is not None else None,
            )
            context._success_deliveries = context._issued_success_deliveries = (*context._success_deliveries, record)
            context.revalidate()
        except BaseException:
            context._failed = True
            raise
        finally:
            # Terminal casts preserve None clearing; these locals are never read again.
            self = cast(_SuccessDelivery, None)
            db = person = message = action_id = context = notification = outbox = record = r = None
            key = cast(str, None)
            html = created = None


_ADMISSIONS_INTAKE = _Operation("web.applicant_intake", "web", frozenset({"admin"}))
_ADMISSIONS_REVIEW = _Operation("web.applicant_review", "web", frozenset({"admin"}))
_ADMISSIONS_TRANSITION = _Operation("api.applicant_transition", "api", frozenset({"admin"}))
_ADMISSIONS_ENROLL = _Operation("api.applicant_enroll", "api", frozenset({"admin"}))
_ADMISSIONS_OPERATIONS = (_ADMISSIONS_INTAKE, _ADMISSIONS_REVIEW, _ADMISSIONS_TRANSITION, _ADMISSIONS_ENROLL)


@dataclass(frozen=True, slots=True)
class _AdmissionsResources:
    entity_id: UUID
    action: str = ""
    reason: str | None = None
    cohort_id: UUID | None = None
    track_id: UUID | None = None
    extend_access: bool = False
    party_id: None = None
    emails: tuple = ()


def _admissions_values(resources):
    if (
        type(resources.entity_id) is not UUID
        or type(resources.action) is not str
        or (resources.reason is not None and type(resources.reason) is not str)
        or any(value is not None and type(value) is not UUID for value in (resources.cohort_id, resources.track_id))
        or type(resources.extend_access) is not bool
        or resources.party_id is not None
        or resources.emails != ()
    ):
        raise HumanOperationError("admission command binding differs")
    return (
        str(resources.entity_id),
        resources.action,
        resources.reason,
        str(resources.cohort_id) if resources.cohort_id else None,
        str(resources.track_id) if resources.track_id else None,
        resources.extend_access,
    )


class _AdmissionState:
    """Exact issued human command, ordered trace and actual domain consequences."""

    extended_access: bool
    receipts: tuple[_AuditReceipt, ...]
    _issued_receipts: tuple[_AuditReceipt, ...]
    deliveries: tuple[tuple[str, bool], ...]
    _issued_deliveries: tuple[tuple[str, bool], ...]

    def __init__(self, context):
        self.context = self._context = context
        self.applicant = None
        self.started = self.executing = False
        self.outcome = self._issued_outcome = None
        self.receipts = self._issued_receipts = ()
        self.deliveries = self._issued_deliveries = ()
        self.base_url = str(context.request.base_url).rstrip("/")
        self.target_id = None
        self.identity = self.original_roles = None
        self.discovery = self._discover()
        self.expected_binding = self.discovery
        self.affected = (
            frozenset(value for value in self.discovery[1:3] if value is not None) if self.discovery else frozenset()
        )

    def _discover(self):
        c = self.context
        row = c.db.execute(
            select(Applicant.email, Applicant.person_id, Applicant.cohort_id, Applicant.track_id).where(
                Applicant.tenant_id == c.tenant_id, Applicant.id == c.resources.entity_id
            )
        ).one_or_none()
        if row is None:
            return None
        target = store.find_party_exact_email(c.db, tenant_id=c.tenant_id, email=row.email)
        return (row.email, row.person_id, target.id if target else None, row.cohort_id, row.track_id)

    def require(self, *, executing=False):
        c = self.context
        c.require()
        if (
            type(self) is not _AdmissionState
            or self.context is not self._context
            or c._admission is not self
            or c.operation not in _ADMISSIONS_OPERATIONS
            or str(c.request.base_url).rstrip("/") != self.base_url
            or (executing and not self.executing)
        ):
            c._failed = True
            raise HumanOperationError("private admission origin differs")

    def prove(self):
        c = self.context
        # Actor authority is proved before revealing target absence.
        if self.discovery is None:
            raise NotFoundError("Applicant not found.")
        self.applicant = onboarding._lock_applicant(c.db, tenant_id=c.tenant_id, applicant_id=c.resources.entity_id)
        onboarding._lock_tasks(c.db, tenant_id=c.tenant_id, applicant_id=c.resources.entity_id)
        self._binding()
        # Refresh and lock both existing and requested canonical placement rows.
        for model, keys in (
            (Cohort, {self.applicant.cohort_id, c.resources.cohort_id}),
            (Track, {self.applicant.track_id, c.resources.track_id}),
        ):
            for key in sorted(value for value in keys if value is not None):
                c._row(model, model.tenant_id == c.tenant_id, model.id == key)
        for cohort_id, track_id in sorted(
            {(self.applicant.cohort_id, self.applicant.track_id), (c.resources.cohort_id, c.resources.track_id)},
            key=str,
        ):
            if cohort_id is not None and track_id is not None:
                c._row(
                    CohortTrack,
                    CohortTrack.tenant_id == c.tenant_id,
                    CohortTrack.cohort_id == cohort_id,
                    CohortTrack.track_id == track_id,
                )
        if c.operation is _ADMISSIONS_ENROLL and self.discovery[2] is not None:
            c.db.scalars(
                select(Enrollment)
                .where(
                    Enrollment.tenant_id == c.tenant_id,
                    Enrollment.person_id == self.discovery[2],
                    Enrollment.cohort_id == c.resources.cohort_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            ).all()
        c._refresh_authority()  # Auth clock and later policy selection follow every blocking lock.
        self.start = _persisted_values(self.applicant)
        self.expected_trace = self._trace()
        self.expected_delivery = (
            "onboarding_invite"
            if (c.operation is _ADMISSIONS_TRANSITION and c.resources.action == "onboarding")
            or (c.operation is _ADMISSIONS_REVIEW and c.resources.action == "accept")
            else "entrance_invite"
            if c.operation is _ADMISSIONS_REVIEW
            and c.resources.action in {"resend_invitation", "reset_assessment", "reinvite_assessment"}
            else None
        )
        self.before_outbox_ids = frozenset(row.id for row in self._outbox())
        self.before_audit_ids = frozenset(
            c.db.scalars(
                select(AuditEvent.id).where(
                    AuditEvent.tenant_id == c.tenant_id,
                    AuditEvent.entity_type == "applicant",
                    AuditEvent.entity_id == str(c.resources.entity_id),
                )
            ).all()
        )
        self.initial = self._collect()

    def _binding(self):
        if self._discover() != self.expected_binding:
            raise ConflictError("Admission identity or intake changed; retry.")

    def _trace(self):
        c, a, r = self.context, cast(Applicant, self.applicant), self.context.resources
        if c.operation is _ADMISSIONS_INTAKE:
            return ("applicant.intake_assigned",)
        if c.operation is _ADMISSIONS_TRANSITION:
            return () if r.action == a.status else ("applicant.transition",)
        if c.operation is _ADMISSIONS_ENROLL:
            return ("applicant.transition",)
        if r.action == "accept":
            self.status_path = {
                "applied": ("screened", "accepted", "onboarding"),
                "screened": ("accepted", "onboarding"),
                "waitlisted": ("accepted", "onboarding"),
                "accepted": ("onboarding",),
            }.get(a.status, ())
            return ("applicant.transition",) * len(self.status_path)
        return (
            {
                "waitlist": "applicant.transition",
                "reject": "applicant.transition",
                "resend_invitation": "applicant.invitation_resent",
                "extend_access": "applicant.access_extended",
                "reset_assessment": "applicant.assessment_reset",
                "reinvite_assessment": "applicant.assessment_reinvite",
            }.get(r.action, ""),
        )

    def expected_details(self, action):
        path: tuple[str, ...]
        c, a, r = self.context, cast(Applicant, self.applicant), self.context.resources
        old = dict(self.start)
        reason, source = r.reason or "", "admin_api" if c.operation.transport == "api" else "admin_web"
        if action == "applicant.transition":
            index = len(self.receipts)
            if c.operation is _ADMISSIONS_ENROLL:
                path = ("enrolled",)
            elif c.operation is _ADMISSIONS_REVIEW:
                path = (
                    self.status_path
                    if r.action == "accept"
                    else ("waitlisted" if r.action == "waitlist" else "rejected",)
                )
            else:
                path = (r.action,)
            previous = old["status"] if index == 0 else path[index - 1]
            if a.status != path[index]:
                raise HumanOperationError("admission transition outcome differs")
            return {"from_status": previous, "to_status": path[index], "reason": reason, "source": source}
        if action == "applicant.intake_assigned":
            track = c.db.get(Track, r.track_id)
            if track is None or (a.cohort_id, a.track_id, a.program) != (r.cohort_id, r.track_id, track.name):
                raise HumanOperationError("admission intake outcome differs")
            return {
                "from_cohort_id": str(old["cohort_id"]) if old["cohort_id"] else "",
                "from_track_id": str(old["track_id"]) if old["track_id"] else "",
                "to_cohort_id": str(r.cohort_id),
                "to_track_id": str(r.track_id),
                "track_name": track.name,
                "reason": reason,
                "source": "admin_web",
            }
        if action in {"applicant.invitation_resent", "applicant.access_extended"}:
            previous = old["assessment_deadline"]
            details = {
                "reason": reason,
                "source": "admin_web",
                "previous_assessment_deadline": previous.isoformat() if previous else "",
                "assessment_deadline": a.assessment_deadline.isoformat() if a.assessment_deadline else "",
            }
            if action == "applicant.invitation_resent":
                details["extended_access"] = self.extended_access
            return details
        return {"reason": reason, "source": "admin_web"}

    def issue(self, action, details):
        row: AuditEvent | None
        self.require(executing=True)
        c = self.context
        try:
            index = len(self.receipts)
            if index >= len(self.expected_trace) or action != self.expected_trace[index]:
                raise HumanOperationError("admission audit order differs")
            if details != self.expected_details(action):
                raise HumanOperationError("admission audit payload differs")
            c._refresh_authority()
            self._binding()
            email = c.parties[c.actor_id].email
            expected = dict(
                tenant_id=c.tenant_id,
                actor_type="user",
                actor_id=str(c.actor_id),
                actor_party_id=c.actor_id,
                actor_label=email if len(email) <= 160 else None,
                source_application="dotmac_academy_app",
                action=action,
                entity_type="applicant",
                entity_id=str(c.resources.entity_id),
                details=deepcopy(details),
                is_success=None,
                request_id=None,
                status_code=None,
                ip_address=None,
                user_agent=None,
            )
            # Let the canonical writer own occurred_at; preserve its exact persisted value.
            row = write_audit_event(c.db, **expected)
            c.db.flush()
            c.db.refresh(row)
            if (
                type(row.id) is not UUID
                or row.id in self.before_audit_ids
                or any(getattr(row, key) != value for key, value in expected.items())
                or row.occurred_at is None
                or row.created_at is None
            ):
                raise HumanOperationError("admission canonical audit receipt differs")
            self.receipts = self._issued_receipts = (*self.receipts, (row, row.id, _persisted_values(row)))
        except BaseException:
            c._failed = True
            raise
        finally:
            row = None
            expected = cast(dict[str, object], None)
            email = c = details = None
            self = cast(_AdmissionState, None)

    def delivery(self, kind, queued):
        self.require(executing=True)
        if kind != self.expected_delivery or type(queued) is not bool or self.deliveries:
            self.context._failed = True
            raise HumanOperationError("admission delivery result differs")
        self.deliveries = self._issued_deliveries = ((kind, queued),)

    def _outbox(self):
        c = self.context
        return c.db.scalars(
            select(EmailOutbox)
            .where(
                EmailOutbox.tenant_id == c.tenant_id,
                (EmailOutbox.payload["applicant_id"].astext == str(c.resources.entity_id))
                | (
                    (EmailOutbox.kind == "onboarding_invite")
                    & EmailOutbox.idempotency_key.startswith(f"onboarding-invite:{c.resources.entity_id}:")
                ),
            )
            .order_by(EmailOutbox.id)
            .execution_options(populate_existing=True)
        ).all()

    def _identity_state(self):
        c = self.context
        values = []
        for model, key in (
            (Party, Party.id),
            (PartyPerson, PartyPerson.party_id),
            (AcademyAccountProfile, AcademyAccountProfile.party_id),
        ):
            predicates = [key == self.target_id]
            if model is not PartyPerson:
                predicates.append(cast(type[Party] | type[AcademyAccountProfile], model).tenant_id == c.tenant_id)
            row = c._row(model, *predicates)
            values.append(_persisted_values(row) if row is not None else None)
        return tuple(values)

    def _roles(self):
        c = self.context
        return (
            tuple(
                _persisted_values(row)
                for row in c.db.scalars(
                    select(Role)
                    .where(Role.tenant_id == c.tenant_id)
                    .order_by(Role.id)
                    .execution_options(populate_existing=True)
                ).all()
            ),
            tuple(
                _persisted_values(row)
                for row in c.db.scalars(
                    select(PartyRoleGrant)
                    .where(PartyRoleGrant.tenant_id == c.tenant_id, PartyRoleGrant.party_id == self.target_id)
                    .order_by(PartyRoleGrant.id)
                    .execution_options(populate_existing=True)
                ).all()
            ),
        )

    def _credentials(self):
        c = self.context
        rows = c.db.scalars(
            select(UserCredential)
            .where(UserCredential.tenant_id == c.tenant_id, UserCredential.party_id == self.target_id)
            .order_by(UserCredential.id)
            .execution_options(populate_existing=True)
        ).all()
        policies = c.db.scalars(
            select(AcademyCredentialPolicy)
            .where(
                AcademyCredentialPolicy.tenant_id == c.tenant_id,
                AcademyCredentialPolicy.credential_id.in_([row.id for row in rows]),
            )
            .order_by(AcademyCredentialPolicy.credential_id)
            .execution_options(populate_existing=True)
        ).all()
        return (tuple(_persisted_values(row) for row in rows), tuple(_persisted_values(row) for row in policies))

    def _collect(self):
        try:
            c = self.context
            c.db.refresh(self.applicant)
            tasks = onboarding._lock_tasks(c.db, tenant_id=c.tenant_id, applicant_id=c.resources.entity_id)
            outbox = self._outbox()
            enrollments = (
                c.db.scalars(
                    select(Enrollment)
                    .where(
                        Enrollment.tenant_id == c.tenant_id,
                        Enrollment.person_id.in_(self.affected | ({self.target_id} if self.target_id else set())),
                    )
                    .execution_options(populate_existing=True)
                ).all()
                if self.affected or self.target_id
                else []
            )
            return (
                _persisted_values(self.applicant),
                tuple(_persisted_values(row) for row in tasks),
                tuple(_persisted_values(row) for row in outbox),
                tuple(sorted((_persisted_values(row) for row in enrollments), key=str)),
                self._identity_state() if self.target_id else None,
                self._roles() if self.target_id else None,
                self._credentials() if self.target_id else None,
            )

        finally:
            self = cast(_AdmissionState, None)
            c = tasks = outbox = enrollments = None

    def execute(self):
        c = self.context
        result = None
        try:
            self.require()
            c.revalidate()
            if self.started:
                raise HumanOperationError("admission command already consumed")
            self.started = self.executing = True
            result = admissions._execute_human_admission(c)
            c.db.flush()
            if result is not self.applicant or len(self.receipts) != len(self.expected_trace):
                raise HumanOperationError("admission command or audit trace incomplete")
            self._binding()
            if len(self.deliveries) != int(self.expected_delivery is not None):
                raise HumanOperationError("admission required delivery observation missing")
            if self.deliveries:
                kind, queued = self.deliveries[0]
                digest = (
                    self.applicant.assessment_token_hash
                    if kind == "entrance_invite"
                    else self.applicant.onboarding_token_hash
                )
                prefix = "entrance-invite" if kind == "entrance_invite" else "onboarding-invite"
                rows = [
                    row for row in self._outbox() if row.idempotency_key == f"{prefix}:{self.applicant.id}:{digest}"
                ]
                if queued and len(rows) != 1:
                    raise HumanOperationError("admission required queued consequence missing")
                if any(
                    row.kind != kind
                    or row.recipient != self.applicant.email
                    or row.payload != ({"applicant_id": str(self.applicant.id)} if kind == "entrance_invite" else {})
                    for row in rows
                ):
                    raise HumanOperationError("admission delivery consequence differs")
            self.outcome = self._issued_outcome = self._collect()
            self.executing = False
            c.revalidate()
            return result
        except BaseException:
            c._failed = True
            if type(c._issued) is _Context:
                c._issued._failed = True
            raise
        finally:
            self = cast(_AdmissionState, None)
            c = result = None
            kind = cast(str, None)
            queued = cast(bool, None)
            digest = None
            prefix = cast(str, None)
            rows = cast(list[EmailOutbox], None)

    def revalidate(self):
        row: AuditEvent | None
        values: _PersistedValues | None
        try:
            self.require()
            if self.executing:
                raise HumanOperationError("admission command cannot expose an incomplete result")
            self._binding()
            expected = self.initial if self.outcome is None else self.outcome
            if (
                self.outcome is not self._issued_outcome
                or self.receipts is not self._issued_receipts
                or self.deliveries is not self._issued_deliveries
            ):
                raise HumanOperationError("admission outcome binding differs")
            if self._collect() != expected:
                raise HumanOperationError("admission persisted consequence differs")
            for row, key, values in self.receipts:
                self.context.db.refresh(row)
                if row.id != key or any(getattr(row, name) != value for name, value in values):
                    raise HumanOperationError("admission persisted audit receipt differs")
            if self.outcome is not None and (not self.started or len(self.receipts) != len(self.expected_trace)):
                raise HumanOperationError("admission ordered trace differs")

        finally:
            self = cast(_AdmissionState, None)
            expected = row = values = None

    def clear(self):
        self.__dict__.clear()


@dataclass(frozen=True, slots=True)
class _Resources:
    entity_id: UUID | None = None
    party_id: UUID | None = None
    role_id: UUID | None = None
    cohort_id: UUID | None = None
    track_id: UUID | None = None
    emails: tuple[str, ...] = ()
    values: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _SchedulingResources:
    entity_id: UUID
    supplied: tuple[tuple[str, str | datetime | None], ...]
    party_id: None = None
    emails: tuple = ()


def _scheduling_values(resources):
    allowed: set[str] | None
    values: list[tuple[str, type, str | None]] | None
    try:
        if (
            type(resources.entity_id) is not UUID
            or type(resources.supplied) is not tuple
            or resources.party_id is not None
            or resources.emails != ()
        ):
            raise HumanOperationError("scheduling resource binding differs")
        allowed = {"title", "starts_at", "ends_at", "session_type", "location", "join_url", "notes", "status"}
        values = []
        for item in resources.supplied:
            if type(item) is not tuple or len(item) != 2:
                raise HumanOperationError("scheduling supplied fields differ")
            key, value = item
            if key not in allowed or any(previous[0] == key for previous in values):
                raise HumanOperationError("scheduling supplied fields differ")
            if value is not None and type(value) not in (str, datetime):
                raise HumanOperationError("scheduling supplied value type differs")
            values.append((key, type(value), value.isoformat() if type(value) is datetime else value))
        return (str(resources.entity_id), tuple(values))
    finally:
        resources = item = key = value = values = allowed = None


def _resource_values(resources):
    values: list[str | None] | None
    identifiers = values = value = email = None
    try:
        if type(resources) is _AdmissionsResources:
            return _admissions_values(resources)
        if type(resources) is _SuccessResources:
            return _success_values(resources)
        if type(resources) is _SchedulingResources:
            return _scheduling_values(resources)
        identifiers = (
            resources.entity_id,
            resources.party_id,
            resources.role_id,
            resources.cohort_id,
            resources.track_id,
        )
        values = []
        for value in identifiers:
            if value is not None and type(value) is not UUID:
                raise HumanOperationError("operation resource identifier type differs")
            values.append(str(value) if value is not None else None)
        if type(resources.emails) is not tuple:
            raise HumanOperationError("operation resource email type differs")
        for email in resources.emails:
            if type(email) is not str:
                raise HumanOperationError("operation resource email type differs")
        # Independent immutable scalar values, never a second alias of the DTO.
        if type(resources.values) is not tuple or any(type(item) is not str for item in resources.values):
            raise HumanOperationError("operation command values differ")
        return (*values, tuple(resources.emails), tuple(resources.values))
    finally:
        resources = identifiers = values = value = email = None


def _operation_values(operation):
    try:
        return (
            operation.name,
            operation.transport,
            tuple(sorted(operation.roles)),
            operation.action,
            operation.entity_type,
        )
    finally:
        operation = None


def _request_digest(request, operation):
    scheme: str | None
    _: str | None
    raw = headers = None
    try:
        headers = Headers(scope=request.scope)
        if operation.transport == "api":
            if request.headers.get("authorization", "") != headers.get("authorization", ""):
                raise HumanOperationError("issued API header cache differs")
            scheme, _, raw = headers.get("authorization", "").partition(" ")
            if scheme.lower() != "bearer" or not raw:
                raise HumanOperationError("issued API credential differs")
        else:
            raw = cookie_parser(headers.get("cookie", "")).get(COOKIE)
            if not raw or raw != request.cookies.get(COOKIE):
                raise HumanOperationError("issued WEB credential differs")
        return hash_token(raw)
    finally:
        request = operation = raw = headers = scheme = _ = None


def _root(db, root):
    if (
        root is None
        or not db.is_active
        or db.get_transaction() is not root
        or conflict_savepoint_requires_rollback(db, expected_root=root)
    ):
        raise HumanOperationError("human operation requires caller transaction rollback") from None


class _Context:
    _scheduling_outcome: _SchedulingOutcome | None
    _issued_scheduling_outcome: _SchedulingOutcome | None
    _success_outcome: _SuccessOutcome | None
    _issued_success_outcome: _SuccessOutcome | None
    _created_grant: _CreatedGrant | None
    _deleted_person: _DeletedPerson | None
    _issued_deleted_person: _DeletedPerson | None
    roles: frozenset[str]
    _success_deliveries: tuple[_SuccessDeliveryRecord, ...]
    _issued_success_deliveries: tuple[_SuccessDeliveryRecord, ...]

    def __init__(self, db, request, operation, resources, tenant_id, digest, candidate, claims):
        try:
            self._issued = self
            self.db = self._db = db
            self.request = self._request = request
            self.root = self._root = db.get_transaction()
            self.operation = self._operation = operation
            self._issued_operation_values = _operation_values(operation)
            self.resources = self._resources = resources
            self._issued_resource_values = _resource_values(resources)
            self.tenant_id = self._tenant_id = tenant_id
            self.actor_id, self.session_id = candidate.party_id, candidate.id
            self._actor_id, self._session_id = self.actor_id, self.session_id
            self.digest = self._digest = digest
            self.claims = self._claims = claims
            self.evidence = self._evidence = None
            self.parties = {}
            self.audits = []
            self.expected_grant_id = None
            self._created_person = self._created_role = self._created_grant = self._deleted_person = None
            self._issued_deleted_person = self._delete_authority = None
            self._audit_receipts = []
            self._scheduling_started = False
            self._scheduling_outcome = self._issued_scheduling_outcome = None
            self._success_prior_audit_ids = None
            self._success_started = False
            self._success_outcome = self._issued_success_outcome = None
            self._success_deliveries = self._issued_success_deliveries = ()
            self._success_delivery = self._issued_success_delivery = None
            self._admission = self._issued_admission = None
            self._failed = False
            self.person = self.profile = self.session = None
            self.roles = frozenset()
            self.now = None
        except BaseException:
            self.__dict__.clear()
            raise
        finally:
            self = cast(_Context, None)
            db = request = operation = resources = tenant_id = digest = candidate = claims = None

    def require(self):
        try:
            if (
                type(self) is not _Context
                or self is not self._issued
                or self.db is not self._db
                or self.request is not self._request
                or self.operation is not self._operation
                or _operation_values(self.operation) != self._issued_operation_values
                or self.resources is not self._resources
                or _resource_values(self.resources) != self._issued_resource_values
                or self.tenant_id != self._tenant_id
                or self.actor_id != self._actor_id
                or self.session_id != self._session_id
                or self.root is not self._root
                or self.digest != self._digest
                or self.claims is not self._claims
                or self.evidence is None
                or self.evidence is not self._evidence
                or self._failed
                or (self.operation in _ADMISSIONS_OPERATIONS and self._admission is not self._issued_admission)
                or require_tenant(self.request).id != self.tenant_id
            ):
                raise HumanOperationError("private human operation binding differs")
            if not hmac.compare_digest(_request_digest(self.request, self.operation), self._digest):
                raise HumanOperationError("issued request credential differs")
            _root(self.db, self.root)
            # Actual Kernel schema contract; neither a new GUC nor a getter API.
            if self.db.scalar(select(func.current_setting("app.current_tenant", True))) != str(self.tenant_id):
                raise HumanOperationError("human operation tenant scope differs")
        except BaseException:
            self._failed = True
            issued, operation = getattr(self, "_issued", None), getattr(self, "operation", None)
            if operation in (*_LEARNING_OPERATIONS, *_ADMISSIONS_OPERATIONS) and type(issued) is _Context:
                issued._failed = True
            if operation in _ADMISSIONS_OPERATIONS and self is not issued:
                self.__dict__.clear()
            raise
        finally:
            self = cast(_Context, None)
            issued = operation = None

    def _affected(self):
        query: Select[tuple[UUID]] | None
        try:
            r = self.resources
            if self.operation in _ADMISSIONS_OPERATIONS:
                return cast(_AdmissionState, self._admission).affected
            query = select(Party.id).where(Party.tenant_id == self.tenant_id)
            if self.operation is _PERSON_CREATE:
                query = query.where(Party.email == r.emails[0])
            elif r.emails:
                query = query.where(func.lower(func.btrim(Party.email)).in_(r.emails))
            elif r.party_id is not None:
                query = query.where(Party.id == r.party_id)
            else:
                return frozenset()
            return frozenset(self.db.scalars(query).all())
        finally:
            self = cast(_Context, None)
            r = query = None

    def _row(self, model, *predicates):
        try:
            return self.db.scalars(
                select(model).where(*predicates).with_for_update().execution_options(populate_existing=True)
            ).first()
        finally:
            self = cast(_Context, None)
            model = None
            predicates = cast(tuple[object, ...], None)

    def prove(self):
        try:
            if self.operation in _ADMISSIONS_OPERATIONS:
                self._admission = self._issued_admission = _AdmissionState(self)
            self.affected = self._affected()  # Discovery only, never authority.
            for key in sorted({self.actor_id, *self.affected}):
                row = self._row(Party, Party.tenant_id == self.tenant_id, Party.id == key)
                if row is None:
                    raise _Denied()
                self.parties[key] = row
            if self._affected() != self.affected:
                if self.operation is _PERSON_CREATE:
                    raise HTTPException(status_code=409, detail="Email already registered")
                raise HumanOperationError("affected principal membership changed; retry")
            self._refresh_authority()
            self.initial_grant_ids = frozenset(row.id for row in self.grants)
            self.evidence = self._evidence = object()
            self.require()
            if self._admission is not None:
                self._admission.prove()
        finally:
            self = cast(_Context, None)
            key = row = None

    def _refresh_authority(self):
        by_id: dict[UUID, str] | None
        try:
            # All actor + affected Party locks precede subtype/profile/session/grants.
            for row in self.parties.values():
                self.db.refresh(row, with_for_update=True)
            self.person = self._row(PartyPerson, PartyPerson.party_id == self.actor_id)
            self.profile = self._row(
                AcademyAccountProfile,
                AcademyAccountProfile.tenant_id == self.tenant_id,
                AcademyAccountProfile.party_id == self.actor_id,
            )
            if self.operation in (_PERSON_DELETE, _ROLE_GRANT) and self.resources.party_id != self.actor_id:
                target_id = self.resources.party_id
                self._row(PartyPerson, PartyPerson.party_id == target_id)
                self._row(
                    AcademyAccountProfile,
                    AcademyAccountProfile.tenant_id == self.tenant_id,
                    AcademyAccountProfile.party_id == target_id,
                )
            self.session = self._row(
                AuthSession,
                AuthSession.tenant_id == self.tenant_id,
                AuthSession.id == self.session_id,
                AuthSession.party_id == self.actor_id,
                AuthSession.token_hash == self.digest,
            )
            roles = self.db.scalars(
                select(Role)
                .where(Role.tenant_id == self.tenant_id)
                .order_by(Role.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).all()
            grants = self.db.scalars(
                select(PartyRoleGrant)
                .where(PartyRoleGrant.tenant_id == self.tenant_id, PartyRoleGrant.party_id.in_(self.parties))
                .order_by(PartyRoleGrant.party_id, PartyRoleGrant.role_id, PartyRoleGrant.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).all()
            by_id = {row.id: row.slug for row in roles}
            if any(row.role_id not in by_id for row in grants):
                raise HumanOperationError("current actor grant catalog differs")
            self.grants = grants
            self.roles = frozenset(by_id[row.role_id] for row in grants if row.party_id == self.actor_id)
            self.now = datetime.now(UTC)  # Sample only AFTER all blocking locks.
            actor, person, profile, session = self.parties[self.actor_id], self.person, self.profile, self.session
            if (
                actor.id != self.actor_id
                or actor.tenant_id != self.tenant_id
                or actor.party_type != PartyType.person
                or actor.is_active is not True
                or type(actor.email) is not str
                or not actor.email.strip()
                or person is None
                or person.party_id != self.actor_id
                or type(person.first_name) is not str
                or type(person.last_name) is not str
                or profile is None
                or profile.party_id != self.actor_id
                or profile.tenant_id != self.tenant_id
                or profile.status != "active"
                or session is None
                or session.id != self.session_id
                or session.party_id != self.actor_id
                or session.tenant_id != self.tenant_id
                or session.revoked_at is not None
                or session.expires_at <= self.now
                or not hmac.compare_digest(session.token_hash, self.digest)
            ):
                raise _Denied()
            if self.claims is not None:
                subject, tenant, expiry = self.claims
                # Preserve Academy JWT's integer-second exp semantics, including ==.
                if subject != self.actor_id or tenant != str(self.tenant_id) or expiry < int(self.now.timestamp()):
                    raise _Denied()
            if not self.operation.roles.intersection(self.roles):
                raise _Denied(role=True)
        finally:
            self = cast(_Context, None)
            row = roles = grants = by_id = actor = person = profile = session = subject = tenant = expiry = None
            target_id = None

    def revalidate(self):
        try:
            self.require()
            self.db.flush()
            if self._deleted_person is not None:
                self._check_deleted_person()
            else:
                self._refresh_authority()
                expected = self.affected
                if self.operation is _PERSON_CREATE and self._created_person is not None:
                    self._check_created_person()
                    expected = expected | {self.resources.entity_id}
                if self._affected() != expected:
                    if self.operation is _PERSON_CREATE and self._created_person is None:
                        raise HTTPException(status_code=409, detail="Email already registered")
                    raise HumanOperationError("affected principal membership changed")
            if self.operation is _ROLE_CREATE and self._created_role is not None:
                self._check_created_role()
            self._check_audits()
            if self._admission is not None:
                self._admission.revalidate()
            if self._scheduling_outcome is not None:
                self._check_scheduling_outcome()
            if self.operation in _SUCCESS_OPERATIONS:
                self._check_success_deliveries()
                if self._success_outcome is not None:
                    self._check_success_outcome()
            if self.audits and self.operation is _ROLE_GRANT:
                self._check_role_grant()
            self.require()
        except BaseException:
            self._failed = True
            raise
        finally:
            self = cast(_Context, None)

    @property
    def actor_is_admin(self):
        try:
            self.revalidate()
            return "admin" in self.roles
        finally:
            self = cast(_Context, None)

    def audit(self, *, details=None):
        """Append only the action/resource fixed by the issuing factory."""
        row: AuditEvent | None
        try:
            self.revalidate()
            if self.operation in _ADMISSIONS_OPERATIONS:
                raise HumanOperationError("private admission command owns audit issuance")
            if self.operation in _LEARNING_OPERATIONS:
                raise HumanOperationError("Learning owns this audit issuance")
            op, resource = self.operation, self.resources
            if op.action is None or resource.entity_id is None or self.audits:
                raise HumanOperationError("operation has no pending fixed audit action")
            if op is _PERSON_CREATE:
                if self._created_person is None or details != {"email": resource.emails[0]}:
                    raise HumanOperationError("person create audit command differs")
            elif op is _ROLE_CREATE:
                if self._created_role is None or details != {"slug": resource.values[0]}:
                    raise HumanOperationError("role create audit command differs")
            elif op is _PERSON_DELETE:
                target = get_person(self.db, tenant_id=self.tenant_id, person_id=resource.party_id)
                if target is None or details != {"email": target.email}:
                    raise HumanOperationError("person delete audit command differs")
            if op is _ROLE_GRANT:
                self.expected_grant_id = self._check_role_grant()
                if details not in (None, {"role_id": str(resource.role_id)}):
                    raise HumanOperationError("role grant audit details differ")
                details = {"role_id": str(resource.role_id)}
            email = self.parties[self.actor_id].email
            expected = dict(
                tenant_id=self.tenant_id,
                actor_type="user",
                actor_id=str(self.actor_id),
                actor_party_id=self.actor_id,
                actor_label=email if len(email) <= 160 else None,
                source_application="dotmac_academy_app",
                action=op.action,
                entity_type=op.entity_type,
                entity_id=str(resource.entity_id),
                occurred_at=self.now,
                is_success=True,
                details=deepcopy(details or {}),
            )
            row = write_audit_event(self.db, **expected)
            self.db.flush()
            self.audits.append((row, deepcopy(expected)))
            self._audit_receipts.append((row, row.id, deepcopy(expected), row.created_at))
            self._check_audits()
        except BaseException:
            self._failed = True  # Caught audit refusal still aborts boundary exit.
            raise
        finally:
            self = cast(_Context, None)
            details = op = resource = None
            expected = cast(dict[str, object], None)
            row = email = target = None

    def update_session(self, *args, **kwargs):
        """Execute the sole fixed Learning command; callers supply no completion claim."""
        command = attribution = result = snapshot = receipt = row = expected = changes = email = None
        try:
            if args or kwargs:
                raise HumanOperationError("scheduling command accepts no supplied execution or completion")
            self.revalidate()
            if (
                self.operation is not _SCHEDULING_UPDATE
                or self._scheduling_started
                or self._scheduling_outcome is not None
            ):
                raise HumanOperationError("scheduling command already consumed or differs")
            self._scheduling_started = True
            command = learning.UpdateSessionCommand(
                tenant_id=self.tenant_id, session_id=self.resources.entity_id, **dict(self.resources.supplied)
            )
            email = self.parties[self.actor_id].email
            attribution = learning.AttemptAuditContext(
                tenant_id=self.tenant_id,
                actor_type="user",
                actor_id=str(self.actor_id),
                actor_label=email if len(email) <= 160 else None,
                source_application="dotmac_academy_app",
            )
            self.revalidate()
            try:
                result = learning.update_session(
                    self.db, command, audit_context=attribution, actor_party_id=self.actor_id
                )
            except learning.SchedulingBadRequest as exc:
                raise BadRequestError(str(exc)) from None
            except learning.SchedulingNotFound as exc:
                raise NotFoundError(str(exc)) from None
            # Capture independently before returning any owner-controlled objects.
            if type(result) is not learning.SessionUpdate or type(result.session) is not learning.ClassSessionFact:
                raise HumanOperationError("scheduling owner outcome type differs")
            snapshot = tuple(
                deepcopy(getattr(result.session, field.name)) for field in fields(learning.ClassSessionFact)
            )
            changes = tuple((change.field, change.from_value, change.to_value) for change in result.changes)
            if (
                type(result.changes) is not tuple
                or any(type(change) is not learning.SessionChange for change in result.changes)
                or len({change[0] for change in changes}) != len(changes)
                or any(change[0] not in dict(self.resources.supplied) for change in changes)
            ):
                raise HumanOperationError("scheduling owner changes differ")
            receipt = None
            if changes:
                if type(result.audit_event_id) is not UUID:
                    raise HumanOperationError("scheduling owner audit receipt missing")
                row = self.db.get(AuditEvent, result.audit_event_id)
                if row is None:
                    raise HumanOperationError("scheduling owner audit row missing")
                self.db.refresh(row)
                expected = dict(
                    tenant_id=self.tenant_id,
                    actor_type="user",
                    actor_id=str(self.actor_id),
                    actor_party_id=self.actor_id,
                    actor_label=attribution.actor_label,
                    source_application="dotmac_academy_app",
                    action="scheduling.update_session",
                    entity_type="class_session",
                    entity_id=str(self.resources.entity_id),
                    is_success=None,
                    details={"changes": {key: {"from": old, "to": new} for key, old, new in changes}},
                    request_id=None,
                    status_code=None,
                    ip_address=None,
                    user_agent=None,
                )
                if any(getattr(row, key) != value for key, value in expected.items()):
                    raise HumanOperationError("scheduling owner audit attribution differs")
                receipt = (
                    row,
                    row.id,
                    tuple((column.key, deepcopy(getattr(row, column.key))) for column in AuditEvent.__table__.columns),
                )
            elif result.audit_event_id is not None:
                raise HumanOperationError("unchanged scheduling owner returned an audit receipt")
            self._scheduling_outcome = self._issued_scheduling_outcome = (
                result,
                snapshot,
                changes,
                result.audit_event_id,
                receipt,
            )
            # Owner's row lock may have waited; all current authority is refreshed again.
            self.revalidate()
            return result
        except BaseException:
            self._failed = True
            raise
        finally:
            self = cast(_Context, None)
            args = cast(tuple[object, ...], None)
            kwargs = cast(dict[str, object], None)
            command = attribution = result = snapshot = receipt = row = expected = changes = email = None

    def _check_scheduling_outcome(self):
        result = snapshot = changes = audit_id = receipt = persisted = row = issued_id = values = None
        try:
            if (
                self.operation is not _SCHEDULING_UPDATE
                or self._scheduling_outcome is not self._issued_scheduling_outcome
            ):
                raise HumanOperationError("scheduling outcome binding differs")
            result, snapshot, changes, audit_id, receipt = cast("_SchedulingOutcome", self._scheduling_outcome)
            if (
                not self._scheduling_started
                or result.session.id != self.resources.entity_id
                or result.session.tenant_id != self.tenant_id
                or tuple(getattr(result.session, field.name) for field in fields(learning.ClassSessionFact)) != snapshot
                or tuple((change.field, change.from_value, change.to_value) for change in result.changes) != changes
                or result.audit_event_id != audit_id
            ):
                raise HumanOperationError("scheduling owner result differs")
            # Scalar columns bypass stale Academy ORM identity-map values.
            persisted = self.db.execute(
                select(*(getattr(ClassSession, field.name) for field in fields(learning.ClassSessionFact))).where(
                    ClassSession.tenant_id == self.tenant_id, ClassSession.id == self.resources.entity_id
                )
            ).one_or_none()
            if persisted is None or tuple(persisted) != snapshot:
                raise HumanOperationError("scheduling persisted owner fact differs")
            if changes:
                row, issued_id, values = cast("_AuditReceipt", receipt)
                self.db.refresh(row)
                if (
                    row.id != issued_id
                    or issued_id != audit_id
                    or any(getattr(row, key) != value for key, value in values)
                ):
                    raise HumanOperationError("scheduling persisted audit receipt differs")
            elif receipt is not None or audit_id is not None:
                raise HumanOperationError("unchanged scheduling receipt differs")
        except BaseException:
            self._failed = True
            raise
        finally:
            self = cast(_Context, None)
            result = snapshot = changes = audit_id = receipt = persisted = row = issued_id = values = None

    def _success_attribution(self):
        email = self.parties[self.actor_id].email
        return learning.AttemptAuditContext(
            tenant_id=self.tenant_id,
            actor_type="user",
            actor_id=str(self.actor_id),
            actor_label=email if len(email) <= 160 else None,
            source_application="dotmac_academy_app",
        )

    def _success_receipt(self, effect, attribution, details):
        if (
            type(effect.audit_event_id) is not UUID
            or self._success_prior_audit_ids is None
            or effect.audit_event_id in self._success_prior_audit_ids
        ):
            raise HumanOperationError("success owner audit receipt missing or stale")
        row = self.db.get(AuditEvent, effect.audit_event_id)
        if row is None:
            raise HumanOperationError("success owner audit row missing")
        self.db.refresh(row)
        expected = dict(
            tenant_id=self.tenant_id,
            actor_type="user",
            actor_id=str(self.actor_id),
            actor_party_id=self.actor_id,
            actor_label=attribution.actor_label,
            source_application="dotmac_academy_app",
            action=self.operation.action,
            entity_type=self.operation.entity_type,
            entity_id=str(self.resources.entity_id),
            is_success=None,
            details=deepcopy(details),
            request_id=None,
            status_code=None,
            ip_address=None,
            user_agent=None,
        )
        if any(getattr(row, key) != value for key, value in expected.items()):
            raise HumanOperationError("success owner audit attribution differs")
        if row.occurred_at is None or row.created_at is None:
            raise HumanOperationError("success owner audit timestamp missing")
        return (row, row.id, _persisted_values(row))

    def transition_success_entry(self, *args, **kwargs):
        effect = attribution = snapshot = receipt = None
        try:
            if args or kwargs:
                raise HumanOperationError("success command accepts no supplied execution or completion")
            self.revalidate()
            if self.operation not in _SUCCESS_TRANSITIONS or self._success_started:
                raise HumanOperationError("success transition already consumed or differs")
            self._success_started = True
            self._success_prior_audit_ids = frozenset(
                self.db.scalars(
                    select(AuditEvent.id).where(
                        AuditEvent.tenant_id == self.tenant_id,
                        AuditEvent.action == self.operation.action,
                        AuditEvent.entity_type == self.operation.entity_type,
                        AuditEvent.entity_id == str(self.resources.entity_id),
                    )
                ).all()
            )
            attribution = self._success_attribution()
            try:
                effect = learning.transition_success_entry_with_receipt(
                    self.db,
                    tenant_id=self.tenant_id,
                    entry_id=self.resources.entity_id,
                    action=self.resources.action,
                    actor_person_id=self.actor_id,
                    audit_context=attribution,
                )
            except learning.SuccessBadRequest as exc:
                raise BadRequestError(str(exc)) from None
            except learning.SuccessNotFound as exc:
                raise NotFoundError(str(exc)) from None
            if type(effect) is not learning.SuccessTransitionEffect or type(effect.entry) is not learning.QueueFact:
                raise HumanOperationError("success transition owner outcome type differs")
            snapshot = tuple(deepcopy(getattr(effect.entry, field.name)) for field in fields(learning.QueueFact))
            receipt = self._success_receipt(
                effect, attribution, {"reason": effect.entry.reason_kind, "person_id": str(effect.entry.person_id)}
            )
            self._success_outcome = self._issued_success_outcome = (effect, snapshot, effect.audit_event_id, receipt)
            self.revalidate()
            return effect
        except BaseException:
            self._failed = True
            if type(self._issued) is _Context:
                self._issued._failed = True
            raise
        finally:
            self = cast(_Context, None)
            args = cast(tuple[object, ...], None)
            kwargs = cast(dict[str, object], None)
            effect = attribution = snapshot = receipt = None

    def message_success_segment(self, *args, **kwargs):
        effect = attribution = snapshot = receipt = terms = accounts = None
        try:
            if args or kwargs:
                raise HumanOperationError("success command accepts no supplied execution or completion")
            self.revalidate()
            if self.operation is not _SUCCESS_MESSAGE or self._success_started:
                raise HumanOperationError("success message already consumed or differs")
            self._success_started = True
            r = self.resources
            if not r.subject or not r.body:
                raise BadRequestError("subject and message are both required")
            if r.segment not in learning.SEGMENT_KEYS:
                raise BadRequestError(f"unknown segment {r.segment!r}")
            if (
                self.db.scalar(select(Cohort.id).where(Cohort.tenant_id == self.tenant_id, Cohort.id == r.entity_id))
                is None
            ):
                raise NotFoundError("cohort not found")
            # Exactly one Settings snapshot and the product's fixed active population.
            terms = success_terms(self.db)
            accounts = learning.ActiveAccountSelection(
                self.tenant_id,
                select(Party.id)
                .join(PartyPerson, PartyPerson.party_id == Party.id)
                .join(
                    AcademyAccountProfile,
                    (AcademyAccountProfile.party_id == Party.id) & (AcademyAccountProfile.tenant_id == Party.tenant_id),
                )
                .where(
                    Party.tenant_id == self.tenant_id,
                    Party.party_type == PartyType.person,
                    Party.is_active.is_(True),
                    AcademyAccountProfile.status == "active",
                ),
            )
            self._success_prior_audit_ids = frozenset(
                self.db.scalars(
                    select(AuditEvent.id).where(
                        AuditEvent.tenant_id == self.tenant_id,
                        AuditEvent.action == self.operation.action,
                        AuditEvent.entity_type == self.operation.entity_type,
                        AuditEvent.entity_id == str(self.resources.entity_id),
                    )
                ).all()
            )
            attribution = self._success_attribution()
            self._success_delivery = self._issued_success_delivery = _SuccessDelivery(self)
            try:
                effect = learning.message_segment_with_receipt(
                    self.db,
                    tenant_id=self.tenant_id,
                    cohort_id=r.entity_id,
                    segment=r.segment,
                    subject=r.subject,
                    body=r.body,
                    actor_person_id=self.actor_id,
                    terms=terms,
                    accounts=accounts,
                    audit_context=attribution,
                    delivery=self._success_delivery,
                    now=r.now,
                )
            except learning.SuccessBadRequest as exc:
                raise BadRequestError(str(exc)) from None
            except learning.SuccessNotFound as exc:
                raise NotFoundError(str(exc)) from None
            if (
                type(effect) is not learning.SegmentMessageEffect
                or type(effect.result) is not learning.SegmentMessageResult
            ):
                raise HumanOperationError("success message owner outcome type differs")
            snapshot = (effect.result.recipients, effect.result.action_id)
            if (
                type(snapshot[0]) is not int
                or snapshot[0] != len(self._success_deliveries)
                or type(snapshot[1]) is not str
                or len(snapshot[1]) != 12
                or any(char not in "0123456789abcdef" for char in snapshot[1])
                or any(item[1] != snapshot[1] for item in self._success_deliveries)
            ):
                raise HumanOperationError("success message delivery outcome differs")
            receipt = self._success_receipt(
                effect,
                attribution,
                {"segment": r.segment, "recipients": snapshot[0], "subject": r.subject, "action_id": snapshot[1]},
            )
            self._success_outcome = self._issued_success_outcome = (effect, snapshot, effect.audit_event_id, receipt)
            self.revalidate()
            return effect
        except BaseException:
            self._failed = True
            if type(self._issued) is _Context:
                self._issued._failed = True
            raise
        finally:
            self = cast(_Context, None)
            args = cast(tuple[object, ...], None)
            kwargs = cast(dict[str, object], None)
            effect = attribution = snapshot = receipt = terms = accounts = r = None

    def _check_success_deliveries(self):
        if (
            self._success_deliveries is not self._issued_success_deliveries
            or self._success_delivery is not self._issued_success_delivery
            or (
                self._success_delivery is not None
                and (
                    type(self._success_delivery) is not _SuccessDelivery or self._success_delivery._context is not self
                )
            )
        ):
            raise HumanOperationError("success delivery observations binding differs")
        for _person_id, _action_id, notification, nid, nvalues, outbox, oid, ovalues in self._success_deliveries:
            self.db.refresh(notification)
            if notification.id != nid or any(getattr(notification, key) != value for key, value in nvalues):
                raise HumanOperationError("success persisted notification differs")
            if outbox is not None:
                self.db.refresh(outbox)
                if outbox.id != oid or any(
                    getattr(outbox, key) != value for key, value in cast("_PersistedValues", ovalues)
                ):
                    raise HumanOperationError("success persisted outbox differs")

    def _check_success_outcome(self):
        if not self._success_started or self._success_outcome is not self._issued_success_outcome:
            raise HumanOperationError("success owner outcome binding differs")
        effect, snapshot, audit_id, receipt = cast("_SuccessOutcome", self._success_outcome)
        if effect.audit_event_id != audit_id:
            raise HumanOperationError("success owner receipt differs")
        if self.operation in _SUCCESS_TRANSITIONS:
            if (
                type(effect) is not learning.SuccessTransitionEffect
                or type(effect.entry) is not learning.QueueFact
                or effect.entry.id != self.resources.entity_id
                or effect.entry.tenant_id != self.tenant_id
                or tuple(getattr(effect.entry, field.name) for field in fields(learning.QueueFact)) != snapshot
            ):
                raise HumanOperationError("success owner queue outcome differs")
            # Scalar projection of the canonical Learning model bypasses identity-map state.
            persisted = self.db.execute(
                select(*(getattr(SuccessQueueEntry, field.name) for field in fields(learning.QueueFact))).where(
                    SuccessQueueEntry.tenant_id == self.tenant_id, SuccessQueueEntry.id == self.resources.entity_id
                )
            ).one_or_none()
            if persisted is None or tuple(persisted) != snapshot:
                raise HumanOperationError("success persisted owner queue fact differs")
        elif (
            self.operation is not _SUCCESS_MESSAGE
            or type(effect) is not learning.SegmentMessageEffect
            or type(effect.result) is not learning.SegmentMessageResult
            or (effect.result.recipients, effect.result.action_id) != snapshot
            or snapshot[0] != len(self._success_deliveries)
            or any(item[1] != snapshot[1] for item in self._success_deliveries)
        ):
            raise HumanOperationError("success message outcome differs")
        row, issued_id, values = receipt
        self.db.refresh(row)
        if row.id != issued_id or issued_id != audit_id or any(getattr(row, key) != value for key, value in values):
            raise HumanOperationError("success persisted audit receipt differs")

    def _check_role_grant(self):
        try:
            if self.operation is not _ROLE_GRANT or self._created_grant is None:
                raise HumanOperationError("fixed role grant command was not completed")
            resource = self.resources
            # Inventory is a completion fact, not target authority. Initial
            # missing targets still receive the command's original exact 404.
            target = get_person(self.db, tenant_id=self.tenant_id, person_id=resource.party_id)
            if target is None:
                raise HumanOperationError("role grant target Academy inventory differs")
            grant = self.db.scalars(
                select(PartyRoleGrant)
                .join(Role, (Role.id == PartyRoleGrant.role_id) & (Role.tenant_id == PartyRoleGrant.tenant_id))
                .where(
                    PartyRoleGrant.tenant_id == self.tenant_id,
                    PartyRoleGrant.party_id == resource.party_id,
                    PartyRoleGrant.role_id == resource.role_id,
                    Role.tenant_id == self.tenant_id,
                )
                .execution_options(populate_existing=True)
            ).first()
            if (
                grant is None
                or grant.id in self.initial_grant_ids
                or (self.expected_grant_id is not None and grant.id != self.expected_grant_id)
            ):
                raise HumanOperationError("role grant audit outcome differs")
            grant = cast(PartyRoleGrant, grant)
            issued_row, expected = cast("_CreatedGrant", self._created_grant)
            if (
                grant is not issued_row
                or expected[:4] != (self.expected_grant_id, self.tenant_id, resource.party_id, resource.role_id)
                or (grant.id, grant.tenant_id, grant.party_id, grant.role_id, grant.created_at, grant.updated_at)
                != expected
            ):
                raise HumanOperationError("role grant persisted outcome differs")
            return grant.id
        finally:
            self = cast(_Context, None)
            resource = grant = issued_row = None
            expected = cast(tuple[UUID, UUID, UUID, UUID, datetime, datetime], None)
            target = None

    def _check_audits(self):
        try:
            if len(self.audits) != len(self._audit_receipts):
                raise HumanOperationError("canonical audit receipt differs")
            for (row, expected), (issued_row, issued_id, issued_values, issued_created_at) in zip(
                self.audits, self._audit_receipts, strict=True
            ):
                if row is not issued_row or expected != issued_values or issued_id is None:
                    raise HumanOperationError("canonical audit receipt differs")
                self.db.refresh(row)
                if (
                    row.id != issued_id
                    or row.created_at != issued_created_at
                    or any(getattr(row, key) != value for key, value in issued_values.items())
                    or any(
                        getattr(row, key) is not None
                        for key in ("request_id", "status_code", "ip_address", "user_agent")
                    )
                ):
                    raise HumanOperationError("canonical audit outcome differs")
        finally:
            self = cast(_Context, None)
            row = expected = issued_row = issued_id = issued_values = issued_created_at = None

    def _check_created_person(self):
        model: type[UserCredential] | type[AuthSession] | type[PartyRoleGrant] | None
        issued_party: Party | None
        issued_person: PartyPerson | None
        issued_profile: AcademyAccountProfile | None
        party = person = profile = expected = result = None
        try:
            expected, issued_party, issued_person, issued_profile, result = cast("_CreatedPerson", self._created_person)
            self.db.refresh(issued_party)
            self.db.refresh(issued_person)
            self.db.refresh(issued_profile)
            party = self.db.get(Party, expected.id)
            person = self.db.get(PartyPerson, expected.id)
            profile = self.db.get(AcademyAccountProfile, expected.id)
            if (
                party is not issued_party
                or person is not issued_person
                or profile is not issued_profile
                or (expected.email, expected.first_name, expected.last_name)
                != (self.resources.emails[0], *self.resources.values)
                or party.id != self.resources.entity_id
                or party.tenant_id != self.tenant_id
                or party.party_type != PartyType.person
                or party.email != expected.email
                or party.display_name != expected.display_name
                or party.is_active is not expected.is_active
                or party.custom_fields != {}
                or party.created_at != expected.created_at
                or party.updated_at != expected.updated_at
                or person.first_name != expected.first_name
                or person.last_name != expected.last_name
                or profile.tenant_id != self.tenant_id
                or profile.status != expected.profile_status
                or profile.avatar_path is not None
                or profile.prefs != {}
                or profile.created_at != expected.created_at
                or profile.updated_at != expected.updated_at
                or result != ApiPersonView(expected.id, expected.email, expected.first_name, expected.last_name)
            ):
                raise HumanOperationError("created person outcome differs")
            for model in (UserCredential, AuthSession, PartyRoleGrant):
                if self.db.scalar(
                    select(model.id).where(model.tenant_id == self.tenant_id, model.party_id == expected.id)
                ):
                    raise HumanOperationError("person creation unexpectedly created account authority")
        finally:
            self = cast(_Context, None)
            party = person = profile = expected = result = issued_party = issued_person = issued_profile = model = None

    def create_person(self):
        """Execute only this factory's immutable generated-person command."""
        policy = values = result = party = person = profile = fact = None
        try:
            self.revalidate()
            if self.operation is not _PERSON_CREATE or self._created_person is not None or self.audits:
                raise HumanOperationError("person creation command differs")
            if self.affected:
                raise HTTPException(status_code=409, detail="Email already registered")
            if self.db.get(Party, self.resources.entity_id) is not None:
                raise HumanOperationError("generated person identifier is already occupied")
            policy = initial_identity_policy(self.db)  # Exactly one product policy snapshot.
            self.revalidate()  # Policy reads may wait; check authority again before DML.
            if self._affected():
                raise HTTPException(status_code=409, detail="Email already registered")
            first_name, last_name = self.resources.values
            values = store.NewPersonValues(
                id=self.resources.entity_id,
                tenant_id=self.tenant_id,
                email=self.resources.emails[0],
                display_name=person_display_name(first_name, last_name),
                first_name=first_name,
                last_name=last_name,
                is_active=policy.is_active,
                custom_fields={},
                profile_status=policy.profile_status,
                profile_avatar_path=None,
                profile_prefs={},
                created_at=cast(datetime, self.now),
                updated_at=cast(datetime, self.now),
            )
            try:
                fact = store.create_person(self.db, values=values)
            except IntegrityError as exc:
                if getattr(exc.orig, "sqlstate", None) == "23505":
                    raise HTTPException(status_code=409, detail="Email already registered") from None
                raise
            party, person, profile = (
                self.db.get(Party, values.id),
                self.db.get(PartyPerson, values.id),
                self.db.get(AcademyAccountProfile, values.id),
            )
            result = ApiPersonView(values.id, cast(str, values.email), values.first_name, values.last_name)
            if fact != store.PartyFact(
                values.id, self.tenant_id, PartyType.person, values.email, values.display_name, values.is_active
            ):
                raise HumanOperationError("person storage result differs")
            self._created_person = (values, party, person, profile, result)
            self._check_created_person()
            self.audit(details={"email": values.email})
            return result
        except BaseException:
            self._failed = True
            raise
        finally:
            self = cast(_Context, None)
            policy = values = result = party = person = profile = fact = first_name = last_name = None

    def _check_created_role(self):
        row = expected = issued_row = None
        try:
            issued_row, expected = cast("_CreatedRole", self._created_role)
            self.db.refresh(issued_row)
            row = self.db.get(Role, expected[0])
            if (
                row is not issued_row
                or expected[:4] != (self.resources.entity_id, self.tenant_id, *self.resources.values)
                or (row.id, row.tenant_id, row.slug, row.name, row.created_at, row.updated_at) != expected
            ):
                raise HumanOperationError("created role outcome differs")
        finally:
            self = cast(_Context, None)
            row = expected = issued_row = None

    def create_role(self):
        catalog = roles = result = row = None
        try:
            self.revalidate()
            if self.operation is not _ROLE_CREATE or self._created_role is not None or self.audits:
                raise HumanOperationError("role creation command differs")
            catalog = store.lock_roles(self.db, tenant_id=self.tenant_id)
            self.revalidate()
            slug, name = self.resources.values
            if any(role.slug == slug for role in catalog):
                raise HTTPException(status_code=409, detail="Role already exists")
            if self.db.get(Role, self.resources.entity_id) is not None:
                raise HumanOperationError("generated role identifier is already occupied")
            stamp = cast(datetime, self.now)
            try:
                roles = store.provision_missing_roles(
                    self.db,
                    tenant_id=self.tenant_id,
                    catalog=catalog,
                    configured_names={slug: name},
                    missing_role_ids={slug: self.resources.entity_id},
                    created_at=stamp,
                    updated_at=stamp,
                )
            except IntegrityError as exc:
                if getattr(exc.orig, "sqlstate", None) == "23505":
                    raise HTTPException(status_code=409, detail="Role already exists") from None
                raise
            result = next((role for role in roles if role.id == self.resources.entity_id), None)
            if result != store.RoleFact(self.resources.entity_id, self.tenant_id, slug, name):
                raise HumanOperationError("role storage result differs")
            row = self.db.get(Role, self.resources.entity_id)
            self._created_role = (row, (self.resources.entity_id, self.tenant_id, slug, name, stamp, stamp))
            self._check_created_role()
            self.audit(details={"slug": slug})
            return result
        except BaseException:
            self._failed = True
            raise
        finally:
            self = cast(_Context, None)
            catalog = roles = result = row = slug = name = None
            stamp = cast(datetime, None)

    def grant_role(self):
        generated_id: UUID | None
        target = role = grants = result = None
        try:
            self.revalidate()
            if self.operation is not _ROLE_GRANT or self.audits:
                raise HumanOperationError("role grant command differs")
            target = get_person(self.db, tenant_id=self.tenant_id, person_id=self.resources.party_id)
            role = self._row(Role, Role.tenant_id == self.tenant_id, Role.id == self.resources.role_id)
            self.revalidate()
            if target is None or role is None:
                raise HTTPException(status_code=404, detail="Person or role not found")
            grants = store.lock_grants(self.db, tenant_id=self.tenant_id, party_ids=(target.id,))
            self.revalidate()
            if any(grant.role_id == role.id for grant in grants):
                raise HTTPException(status_code=409, detail="Role already assigned")
            generated_id = uuid4()
            if self.db.get(PartyRoleGrant, generated_id) is not None:
                raise HumanOperationError("generated grant identifier is already occupied")
            stamp = cast(datetime, self.now)
            try:
                result = store.ensure_grant(
                    self.db,
                    tenant_id=self.tenant_id,
                    party_id=target.id,
                    role_id=role.id,
                    new_grant_id=generated_id,
                    created_at=stamp,
                    updated_at=stamp,
                )
            except IntegrityError as exc:
                if getattr(exc.orig, "sqlstate", None) == "23505":
                    raise HTTPException(status_code=409, detail="Role already assigned") from None
                raise
            if result != store.GrantFact(generated_id, self.tenant_id, target.id, role.id):
                raise HumanOperationError("role grant storage outcome differs")
            self.expected_grant_id = generated_id
            row = self.db.get(PartyRoleGrant, generated_id)
            self._created_grant = (row, (generated_id, self.tenant_id, target.id, role.id, stamp, stamp))
            self._check_role_grant()
            self.audit()
        except BaseException:
            self._failed = True
            raise
        finally:
            self = cast(_Context, None)
            target = role = grants = result = generated_id = None
            stamp = cast(datetime, None)
            row = None

    def delete_person(self):
        """Audit and delete the exact target; only this command issues its completion."""
        target = expiry = receipt = None
        try:
            self.revalidate()
            if self.operation is not _PERSON_DELETE or self._deleted_person is not None or self.audits:
                raise HumanOperationError("person deletion command differs")
            target = get_person(self.db, tenant_id=self.tenant_id, person_id=self.resources.party_id)
            if target is None:
                raise HTTPException(status_code=404, detail="Person not found")
            # Target subtype/profile locks are after all affected Party locks.
            if (
                store.lock_person(self.db, tenant_id=self.tenant_id, party_id=target.id) is None
                or store.lock_profile(self.db, tenant_id=self.tenant_id, party_id=target.id) is None
            ):
                raise HTTPException(status_code=404, detail="Person not found")
            self.revalidate()
            self.audit(details={"email": target.email})
            receipt = self._audit_receipts[0]
            self.revalidate()  # Fresh genuine predelete authority, after the audit write.
            expiry = cast(AuthSession, self.session).expires_at
            self._delete_authority = (
                self.actor_id,
                self.session_id,
                self.digest,
                tuple(self.claims),
                expiry,
                frozenset(self.roles),
                self.root,
                self.request,
            )
            store.delete_person(self.db, tenant_id=self.tenant_id, party_id=target.id)
            self._deleted_person = self._issued_deleted_person = (target.id, expiry, receipt)
            self._check_deleted_person()
        except BaseException:
            self._failed = True
            raise
        finally:
            self = cast(_Context, None)
            target = expiry = receipt = None

    def _check_deleted_person(self):
        _model: _IdentityModel | None
        key: InstrumentedAttribute[UUID] | None
        target_id = expiry = receipt = now = None
        try:
            self.require()
            if self.operation is not _PERSON_DELETE:
                raise HumanOperationError("person deletion completion differs")
            target_id, expiry, receipt = cast("_DeletedPerson", self._deleted_person)
            if (
                self._deleted_person is not self._issued_deleted_person
                or self._delete_authority
                != (
                    self.actor_id,
                    self.session_id,
                    self.digest,
                    tuple(self.claims),
                    expiry,
                    frozenset(self.roles),
                    self.root,
                    self.request,
                )
                or target_id != self.resources.party_id
                or receipt is not self._audit_receipts[0]
            ):
                raise HumanOperationError("person deletion outcome binding differs")
            self._check_audits()
            if self._affected() != self.affected - {target_id}:
                raise HumanOperationError("deleted person membership differs")
            for _model, key in (
                (Party, Party.id),
                (PartyPerson, PartyPerson.party_id),
                (AcademyAccountProfile, AcademyAccountProfile.party_id),
                (AuthSession, AuthSession.party_id),
                (PartyRoleGrant, PartyRoleGrant.party_id),
                (UserCredential, UserCredential.party_id),
            ):
                if self.db.scalar(select(key).where(key == target_id)) is not None:
                    raise HumanOperationError("bound deleted account member survived")
            if target_id != self.actor_id:
                # Do not refresh the intentionally deleted target ORM instance.
                self.parties.pop(target_id, None)
                self._refresh_authority()
            else:
                now = datetime.now(UTC)
                if expiry <= now or self.claims is None:
                    raise _Denied()
                subject, tenant, jwt_expiry = self.claims
                if subject != self.actor_id or tenant != str(self.tenant_id) or jwt_expiry < int(now.timestamp()):
                    raise _Denied()
            self.require()
        finally:
            self = cast(_Context, None)
            target_id = expiry = receipt = now = _model = key = subject = tenant = jwt_expiry = None

    def complete(self):
        try:
            self.revalidate()
            if self.operation in _ADMISSIONS_OPERATIONS and (
                self._admission is None or self._admission.outcome is None
            ):
                raise HumanOperationError("fixed admission command was not completed")
            if self.operation in _SUCCESS_OPERATIONS and self._success_outcome is None:
                raise HumanOperationError("fixed success command was not completed")
            if self.operation is _SCHEDULING_UPDATE and self._scheduling_outcome is None:
                raise HumanOperationError("fixed scheduling command was not completed")
            if (
                (self.operation is _PERSON_CREATE and self._created_person is None)
                or (self.operation is _ROLE_CREATE and self._created_role is None)
                or (self.operation is _PERSON_DELETE and self._deleted_person is None)
                or (self.operation is _ROLE_GRANT and self._created_grant is None)
            ):
                raise HumanOperationError("fixed operation command was not completed")
        except BaseException:
            self._failed = True
            raise
        finally:
            self = cast(_Context, None)

    def execute_admission(self, *args, **kwargs):
        try:
            if (
                args
                or kwargs
                or getattr(self, "operation", None) not in _ADMISSIONS_OPERATIONS
                or getattr(self, "_admission", None) is None
            ):
                raise HumanOperationError("admission accepts no supplied execution or completion")
            self.require()
            return cast(_AdmissionState, self._admission).execute()
        except BaseException:
            self._failed = True
            issued = getattr(self, "_issued", None)
            if type(issued) is _Context:
                issued._failed = True
            if self is not issued:
                self.__dict__.clear()
            raise
        finally:
            self = cast(_Context, None)
            args = cast(tuple[object, ...], None)
            kwargs = cast(dict[str, object], None)
            issued = None

    def clear(self):
        if self._admission is not None:
            self._admission.clear()
        self.__dict__.clear()  # Includes evidence, digests, rows, claims, Request.


@contextmanager
def _human_operation(db, *, request, operation, resources):
    scheme: str | None
    _: str | None
    context = raw = digest = claims = candidate = payload = None
    root = db.get_transaction()
    try:
        if not isinstance(request, Request) or not isinstance(db, Session):
            raise HumanOperationError("actual Request and Session are required")
        with conflict_savepoint(db):
            root = db.get_transaction()
            tenant_id = require_tenant(request).id
            if db.scalar(select(func.current_setting("app.current_tenant", True))) != str(tenant_id):
                raise HumanOperationError("human operation tenant scope differs")
            if operation.transport == "api":
                scheme, _, raw = request.headers.get("authorization", "").partition(" ")
                if scheme.lower() != "bearer" or not raw:
                    raise _Denied()
                payload = decode_access_token(raw)
                if payload is None or payload.get("tenant_id") != str(tenant_id):
                    raise _Denied()
                try:
                    claims = (UUID(str(payload["sub"])), payload["tenant_id"], payload["exp"])
                except (KeyError, ValueError, TypeError):
                    raise _Denied() from None
                payload = None
            else:
                raw = request.cookies.get(COOKIE)
                if not raw:
                    raise _Denied()
            digest = hash_token(raw)
            raw = None
            candidate = db.scalars(
                select(AuthSession)
                .where(AuthSession.tenant_id == tenant_id, AuthSession.token_hash == digest)
                .execution_options(populate_existing=True)
            ).first()
            if candidate is None:
                raise _Denied()
            context = _Context(db, request, operation, resources, tenant_id, digest, candidate, claims)
            digest = claims = candidate = None
            context.prove()
            yield context
            context.complete()
            if operation not in _LEARNING_OPERATIONS and operation.action is not None and len(context.audits) != 1:
                raise HumanOperationError("required canonical audit event is missing")
            _root(db, root)
    except BaseException as exc:
        _root(db, root)
        if isinstance(exc, _Denied):
            if exc.role:
                raise HTTPException(status_code=403, detail="Forbidden") from None
            if operation.transport == "web":
                raise HTTPException(status_code=303, headers={"Location": "/login"}) from None
            raise HTTPException(status_code=401, detail="Unauthorized") from None
        if not isinstance(exc, Exception):
            raise
        if isinstance(exc, (HTTPException, BadRequestError, ConflictError, NotFoundError)) or (
            (
                operation is _ROSTER_STATE
                or operation is _REMOVE_FACILITATOR
                or operation in (*_LEARNING_OPERATIONS, *_ADMISSIONS_OPERATIONS)
            )
            and isinstance(exc, TransactionChanged)
        ):
            raise exc from None
        raise HumanOperationError("human operation failed; no operation change was saved") from None
    finally:
        if context is not None:
            context.clear()
        raw = digest = claims = candidate = context = payload = request = operation = resources = scheme = _ = None


def grant_api_role(db, *, request, person_id, role_id):
    try:
        return _human_operation(
            db,
            request=request,
            operation=_ROLE_GRANT,
            resources=_Resources(entity_id=person_id, party_id=person_id, role_id=role_id),
        )
    finally:
        db = request = person_id = role_id = None


def assign_web_facilitator(db, *, request, cohort_id, person_id):
    try:
        return _human_operation(
            db, request=request, operation=_FACILITATOR, resources=_Resources(cohort_id=cohort_id, party_id=person_id)
        )
    finally:
        db = request = cohort_id = person_id = None


def _normalized_emails(emails):
    normalized: dict[str, None] | None
    normalized = raw = value = None
    try:
        normalized = {}
        for raw in emails:
            value = (raw or "").strip().lower()
            if value:
                normalized[value] = None
        return tuple(normalized)
    finally:
        emails = normalized = raw = value = None


def bulk_enroll_web_roster(db, *, request, cohort_id, emails, track_id=None):
    normalized = None
    try:
        normalized = _normalized_emails(emails)
        return _human_operation(
            db,
            request=request,
            operation=_BULK_ENROLL,
            resources=_Resources(cohort_id=cohort_id, track_id=track_id, emails=normalized),
        )
    finally:
        db = request = cohort_id = emails = track_id = normalized = None


def resend_web_account_invitation(db, *, request, person_id):
    try:
        # The invitation owner retains its existing account.invitation.completed audit.
        return _human_operation(
            db, request=request, operation=_RESEND_ACCOUNT, resources=_Resources(party_id=person_id)
        )
    finally:
        db = request = person_id = None


def create_api_person(db, *, request, email, first_name, last_name):
    try:
        return _human_operation(
            db,
            request=request,
            operation=_PERSON_CREATE,
            resources=_Resources(entity_id=uuid4(), emails=(normalize_email(email),), values=(first_name, last_name)),
        )
    finally:
        db = request = email = first_name = last_name = None


def create_api_role(db, *, request, slug, name):
    try:
        return _human_operation(
            db, request=request, operation=_ROLE_CREATE, resources=_Resources(entity_id=uuid4(), values=(slug, name))
        )
    finally:
        db = request = slug = name = None


def delete_api_person(db, *, request, person_id):
    try:
        return _human_operation(
            db, request=request, operation=_PERSON_DELETE, resources=_Resources(entity_id=person_id, party_id=person_id)
        )
    finally:
        db = request = person_id = None


def change_web_roster_state(db, *, request, cohort_id, person_id, state):
    try:
        return _human_operation(
            db,
            request=request,
            operation=_ROSTER_STATE,
            resources=_Resources(cohort_id=cohort_id, party_id=person_id, values=(state,)),
        )
    finally:
        db = request = cohort_id = person_id = state = None


def remove_web_facilitator(db, *, request, cohort_id, person_id):
    try:
        return _human_operation(
            db,
            request=request,
            operation=_REMOVE_FACILITATOR,
            resources=_Resources(cohort_id=cohort_id, party_id=person_id),
        )
    finally:
        db = request = cohort_id = person_id = None


def update_web_session(db, *, request, session_id, supplied):
    try:
        return _human_operation(
            db,
            request=request,
            operation=_SCHEDULING_UPDATE,
            resources=_SchedulingResources(entity_id=session_id, supplied=tuple(supplied)),
        )
    finally:
        db = request = session_id = supplied = None


def transition_web_success_entry(db, *, request, entry_id, action):
    operations: dict[str, _Operation] | None
    try:
        operations = {"acknowledge": _SUCCESS_ACKNOWLEDGE, "assign": _SUCCESS_ASSIGN, "resolve": _SUCCESS_RESOLVE}
        if action not in operations:
            raise BadRequestError(f"unknown success transition {action!r}")
        return _human_operation(
            db,
            request=request,
            operation=operations[action],
            resources=_SuccessResources(entity_id=entry_id, action=action),
        )
    finally:
        db = request = entry_id = action = operations = None


def message_web_success_segment(db, *, request, cohort_id, segment, subject, body, now=None):
    try:
        return _human_operation(
            db,
            request=request,
            operation=_SUCCESS_MESSAGE,
            resources=_SuccessResources(
                entity_id=cohort_id,
                segment=segment,
                subject=(subject or "").strip(),
                body=(body or "").strip(),
                now=now,
            ),
        )
    finally:
        db = request = cohort_id = segment = subject = body = now = None


def _require_human_admission(context, db):
    try:
        if type(context) is not _Context or context.db is not db or context.operation not in _ADMISSIONS_OPERATIONS:
            raise HumanOperationError("exact human admission origin required")
        context.require()
        if context._admission is None or context._admission.context is not context:
            raise HumanOperationError("human admission context identity differs")
        context._admission.require(executing=True)
        return context._admission
    except BaseException:
        if type(context) is _Context:
            context._failed = True
            issued = getattr(context, "_issued", None)
            if type(issued) is _Context:
                issued._failed = True
        raise
    finally:
        context = db = issued = None


def assign_web_applicant_intake(db, *, request, applicant_id, cohort_id, track_id, reason=None):
    return _human_operation(
        db,
        request=request,
        operation=_ADMISSIONS_INTAKE,
        resources=_AdmissionsResources(applicant_id, reason=reason, cohort_id=cohort_id, track_id=track_id),
    )


def review_web_applicant(db, *, request, applicant_id, action, reason=None, extend_access=False):
    return _human_operation(
        db,
        request=request,
        operation=_ADMISSIONS_REVIEW,
        resources=_AdmissionsResources(applicant_id, action=action, reason=reason, extend_access=extend_access),
    )


def transition_api_applicant(db, *, request, applicant_id, to_status, notes=None):
    return _human_operation(
        db,
        request=request,
        operation=_ADMISSIONS_TRANSITION,
        resources=_AdmissionsResources(applicant_id, action=to_status, reason=notes),
    )


def enroll_api_applicant(db, *, request, applicant_id, cohort_id):
    return _human_operation(
        db,
        request=request,
        operation=_ADMISSIONS_ENROLL,
        resources=_AdmissionsResources(applicant_id, cohort_id=cohort_id),
    )
