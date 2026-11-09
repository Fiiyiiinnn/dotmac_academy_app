"""Genuine assessment-bearer submit boundary, prepared SOURCE ONLY.

The Request's actual form and the locked current Applicant are the only submit
inputs. Returned observations confer no writer authority. Private completion
pins live until the caller's exact root ends, including its before-commit check.
Historical accepted-result preservation and ERP assessment receipt qualification
remain separate gates; this boundary creates no replacement result store.
"""

# Ported at new-composition genesis (web layer wave).

from __future__ import annotations

import hmac
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from uuid import UUID
from weakref import WeakKeyDictionary

from dotmac_kernel.audit import AuditEvent, write_audit_event
from dotmac_kernel.transactions import (
    ConflictSavepointState,
    claim_conflict_savepoint_commit,
    conflict_savepoint,
    conflict_savepoint_requires_rollback,
    current_conflict_savepoint,
)
from dotmac_learning import TransactionChanged
from dotmac_learning.curriculum_models import Question, QuestionBank
from dotmac_learning.participation_models import Cohort, CohortTrack, Track
from fastapi import Request
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session, SessionTransaction

from app.api.deps import require_tenant
from app.models.admissions import Applicant
from app.models.email_outbox import EmailOutbox
from app.models.entrance_defaults import TenantEntranceDefaults
from app.models.onboarding import OnboardingTask
from app.services import admissions, applicant_email, entrance_exam, settings_store
from app.services.admission_assessment_policy import assessment_path, below_threshold_reason
from app.services.exceptions import BadRequestError, NotFoundError
from app.services.security import hash_token


class AssessmentSubmissionError(Exception):
    """Refused private operation; no token or answer material in messages."""


class AssessmentGradeRejected(Exception):
    """Only the original grader's ordinary bad-request/not-found UI branch."""


@dataclass(frozen=True, slots=True)
class AssessmentSubmission:
    applicant: object | None
    disposition: str


# Neither Session.info nor a supplied context object establishes authority.
# Root-end cleanup also releases Request, answer, digest and ORM references.
_ROOTS: WeakKeyDictionary[Session, list[_AssessmentContext]] = WeakKeyDictionary()

# Heterogeneous mapped-column snapshots retain each column's actual value.
_Snapshot = tuple[tuple[str, object], ...]
_PinnedRow = (
    Applicant
    | AuditEvent
    | Cohort
    | Track
    | CohortTrack
    | TenantEntranceDefaults
    | QuestionBank
    | Question
    | OnboardingTask
    | EmailOutbox
)
_POLICY_KEYS = (
    "learning_missing_question_weight",
    "learning_exam_seconds_per_question",
    "learning_exam_near_chance_margin",
    "learning_exam_fallback_score_floor",
    "learning_exam_fallback_duration_seconds",
    "entrance_questions_per_category",
    "onboarding_task_catalog",
    "onboarding_enrollment_global_role",
    "web_account_creation_role_provisioning",
)


def _values(row):
    return tuple((c.key, deepcopy(getattr(row, c.key))) for c in row.__table__.columns)


def _registered(context):
    return any(item is context for item in _ROOTS.get(context.db, ()))


class _AssessmentContext:
    def __init__(self, db, request, tenant_id, digest, form):
        self.db = self._db = db
        self.request = self._request = request
        self.root = self._root = db.get_transaction()
        self.connection = db.connection()
        self.physical = self.connection.connection.dbapi_connection
        self.savepoint = cast(ConflictSavepointState, current_conflict_savepoint(db)).savepoint
        self.tenant_id = self._tenant_id = tenant_id
        self.digest = digest
        self.form = form
        self.base_url = str(request.base_url).rstrip("/")
        self.applicant: Applicant | None = None
        self.failed = False
        self.executing = False
        self.finished = False
        self.ended = False
        self.pins: tuple[tuple[_PinnedRow, _Snapshot], ...] = ()
        self.pin: UUID | None = None
        self.bank_version_id: UUID | None = None
        self.inputs: tuple[tuple[_PinnedRow, _Snapshot], ...] = ()
        self.receipts: tuple[tuple[AuditEvent, _Snapshot], ...] = ()
        self.path: tuple[str, ...] = ()
        self.reason = ""
        self.result: _Snapshot | None = None
        self.answers: dict[str, list[str]] | None = None
        self.policy: _Snapshot | None = None

    def require(self):
        try:
            if self.ended or self.db.get_transaction() is not self._root:
                raise TransactionChanged("assessment root transaction changed")
            if (
                type(self) is not _AssessmentContext
                or self.db is not self._db
                or not _registered(self)
                or self.request is not self._request
                or self.tenant_id != self._tenant_id
                or self.failed
                or require_tenant(self.request).id != self.tenant_id
                or str(self.request.base_url).rstrip("/") != self.base_url
                or not hmac.compare_digest(hash_token(str(self.form.get("token") or "")), self.digest)
            ):
                raise AssessmentSubmissionError("assessment private origin differs")
            if (
                self.root is not self._root
                or self.db.get_transaction() is not self._root
                or not self.db.is_active
                or conflict_savepoint_requires_rollback(self.db, expected_root=self._root)
                or self.connection.closed
                or self.connection.invalidated
                or self.db.connection() is not self.connection
                or self.connection.connection.dbapi_connection is not self.physical
            ):
                raise TransactionChanged("assessment transaction changed")
            if self.db.scalar(select(func.current_setting("app.current_tenant", True))) != str(self.tenant_id):
                raise AssessmentSubmissionError("assessment tenant scope differs")
        except BaseException:
            self.failed = True
            raise

    def _policy(self):
        cfg = settings_store.effective(self.db, require_store=True)
        return tuple((key, deepcopy(cfg.get(key))) for key in _POLICY_KEYS)

    def _lock(self, model, *conditions):
        return self.db.scalars(
            select(model).where(*conditions).with_for_update().execution_options(populate_existing=True)
        ).all()

    def _question_conditions(self):
        # Scope the locked paper to the sitting's pinned bank version.
        # Unversioned (legacy) sittings lock the NULL-version rows exactly
        # as Learning's serving read does; other versions' rows (drafts,
        # newer publishes) are not part of this paper.
        conditions = [Question.tenant_id == self.tenant_id, Question.bank_id == self.bank_id]
        if self.bank_version_id is not None:
            conditions.append(Question.bank_version_id == self.bank_version_id)
        else:
            conditions.append(Question.bank_version_id.is_(None))
        return conditions

    def _check_inputs(self):
        self.require()
        for row, expected in self.inputs:
            self.db.refresh(row)
            if _values(row) != expected:
                raise AssessmentSubmissionError("assessment input facts changed")
        # Re-read the membership set, including insertion/deletion, not only rows
        # that existed when the paper was selected.
        ids = tuple(sorted(self.db.scalars(select(Question.id).where(*self._question_conditions())).all()))
        if (
            ids != self.question_ids
            or self._policy() != self.policy
            or entrance_exam.resolve_bank_id(self.db, applicant=cast(Applicant, self.applicant)) != self.bank_id
            or entrance_exam.time_limit_minutes(self.db, applicant=cast(Applicant, self.applicant)) != self.limit
        ):
            raise AssessmentSubmissionError("assessment paper or policy changed")
        self.require()  # Policy/row reads may have blocked.
        a = cast(Applicant, self.applicant)
        if (
            a.tenant_id != self.tenant_id
            or a.id != self.applicant_id
            or a.assessment_token_hash != self.digest
            or a.assessment_bank_version_id != self.pin
            or a.assessment_started_at != self.started_at
            or a.assessment_deadline != self.deadline
            or a.assessment_reset_count != self.reset_count
            or (a.cohort_id, a.track_id) != self.intake
            or entrance_exam.past_deadline(a)
        ):
            raise AssessmentSubmissionError("assessment bearer or sitting changed")

    def prove(self):
        rows = self._lock(
            Applicant, Applicant.tenant_id == self.tenant_id, Applicant.assessment_token_hash == self.digest
        )
        self.require()  # Actual clock/authority follows the Applicant lock wait.
        if not rows or rows[0].assessment_taken_at is not None:
            return "already_completed"
        self.applicant = a = rows[0]
        if entrance_exam.past_deadline(a):
            return "deadline_closed"
        # A normal reset clears content while preserving version/attempt metadata.
        # Hold anomalous partial content; never overwrite its existing evidence.
        if (
            any(
                getattr(a, key) is not None
                for key in (
                    "assessment_score",
                    "assessment_level",
                    "assessment_profile",
                    "assessment_valid",
                    "assessment_invalid_reason",
                    "assessment_erp_synced_at",
                )
            )
            or a.assessment_time_exceeded is True
        ):
            raise AssessmentSubmissionError("existing partial assessment content requires recovery")
        self.applicant_id = a.id
        self.started_at, self.deadline = a.assessment_started_at, a.assessment_deadline
        self.reset_count = a.assessment_reset_count
        self.intake = (a.cohort_id, a.track_id)
        self.start = _values(a)
        locked: list[_PinnedRow] = []
        # Existing intake and default rows own their original policy; no new
        # placement, default, or bank is manufactured by this boundary.
        for model, conditions in (
            (Cohort, (Cohort.tenant_id == self.tenant_id, Cohort.id == a.cohort_id)),
            (Track, (Track.tenant_id == self.tenant_id, Track.id == a.track_id)),
            (
                CohortTrack,
                (
                    CohortTrack.tenant_id == self.tenant_id,
                    CohortTrack.cohort_id == a.cohort_id,
                    CohortTrack.track_id == a.track_id,
                ),
            ),
            (TenantEntranceDefaults, (TenantEntranceDefaults.tenant_id == self.tenant_id,)),
        ):
            locked.extend(self._lock(model, *conditions))
        self.bank_id = entrance_exam.resolve_bank_id(self.db, applicant=a)
        locked.extend(
            self._lock(QuestionBank, QuestionBank.tenant_id == self.tenant_id, QuestionBank.id == self.bank_id)
        )
        # Pin captured under the bank lock: start-time pin wins, else the
        # bank's current published version (legacy best-effort). A
        # versioned bank with no current version refuses here.
        self.pin = a.assessment_bank_version_id
        self.bank_version_id = entrance_exam.effective_bank_version_id(
            self.db, tenant_id=self.tenant_id, applicant=a, bank_id=self.bank_id
        )
        self.questions = self._lock(Question, *self._question_conditions())
        locked.extend(self.questions)
        self.question_ids = tuple(sorted(q.id for q in self.questions))
        self.limit = entrance_exam.time_limit_minutes(self.db, applicant=a)
        self.inputs = tuple((row, _values(row)) for row in locked)
        self.policy = self._policy()
        self._check_inputs()
        self.db.refresh(a)
        if _values(a) != self.start:
            raise AssessmentSubmissionError("assessment current sitting changed")
        self.prior_audits = frozenset(self._audit_rows())
        self.prior_outbox = {row.id: _values(row) for row in self._outbox()}
        self.prior_tasks = {row.id: _values(row) for row in self._tasks()}
        return None

    def _audit_rows(self):
        return self.db.scalars(
            select(AuditEvent.id).where(
                AuditEvent.tenant_id == self.tenant_id,
                AuditEvent.entity_type == "applicant",
                AuditEvent.entity_id == str(self.applicant_id),
            )
        ).all()

    def _tasks(self):
        return self.db.scalars(
            select(OnboardingTask)
            .where(OnboardingTask.tenant_id == self.tenant_id, OnboardingTask.applicant_id == self.applicant_id)
            .order_by(OnboardingTask.id)
            .execution_options(populate_existing=True)
        ).all()

    def _outbox(self):
        return self.db.scalars(
            select(EmailOutbox)
            .where(
                EmailOutbox.tenant_id == self.tenant_id,
                EmailOutbox.idempotency_key.startswith(f"assessment-waitlist:{self.applicant_id}:")
                | EmailOutbox.idempotency_key.startswith(f"assessment-review:{self.applicant_id}:")
                | EmailOutbox.idempotency_key.startswith(f"onboarding-invite:{self.applicant_id}:"),
            )
            .order_by(EmailOutbox.id)
            .execution_options(populate_existing=True)
        ).all()

    def _check_grade(self):
        if self.result is None:
            raise AssessmentSubmissionError("assessment grade owner has not completed")
        current = dict(_values(self.applicant))
        for key, value in self.result:
            if key not in {"status", "notes", "onboarding_token_hash", "updated_at"} and current[key] != value:
                raise AssessmentSubmissionError("assessment grade facts changed")

    def _derive_path(self):
        a = cast(Applicant, self.applicant)
        cohort = self.db.get(Cohort, a.cohort_id) if a.cohort_id else None
        threshold = cohort.auto_accept_threshold if cohort is not None else None
        below_action = cohort.auto_accept_below_action if cohort is not None else "waitlisted"
        self.path = assessment_path(
            status=dict(self.start)["status"],
            score=a.assessment_score,
            valid=a.assessment_valid is True,
            threshold=threshold,
            below_action=below_action,
        )
        self.reason = (
            below_threshold_reason(cast(float, a.assessment_score), cast(float, threshold))
            if self.path == ("waitlisted",)
            else ""
        )

    def _check_tasks(self):
        rows = self._tasks()
        prior = {dict(values)["key"]: dict(values) for values in self.prior_tasks.values()}
        catalog = cast(tuple[tuple[str, str], ...], dict(cast(_Snapshot, self.policy))["onboarding_task_catalog"])
        expected_keys = set(prior) | ({key for key, _ in catalog} if self.path[-1:] == ("onboarding",) else set())
        if {row.key for row in rows} != expected_keys:
            raise AssessmentSubmissionError("assessment onboarding task inventory differs")
        configured = {key: (label, index) for index, (key, label) in enumerate(catalog)}
        for row in rows:
            if row.key in prior:
                old = prior[row.key]
                mutable = (
                    {"status", "completed_at", "updated_at"}
                    if (self.path[-1:] == ("onboarding",) and row.key == "entrance_assessment")
                    else set()
                )
                if any(getattr(row, key) != value for key, value in old.items() if key not in mutable):
                    raise AssessmentSubmissionError("assessment changed unrelated existing task facts")
            elif (
                type(row.id) is not UUID
                or row.tenant_id != self.tenant_id
                or row.applicant_id != self.applicant_id
                or (row.label, row.order_index) != configured[row.key]
            ):
                raise AssessmentSubmissionError("assessment created incorrect onboarding task")
            if self.path[-1:] == ("onboarding",) and row.key == "entrance_assessment":
                if row.status != "done" or row.completed_at is None:
                    raise AssessmentSubmissionError("assessment carry-forward task was not completed")
            elif row.key not in prior and (row.status != "pending" or row.completed_at is not None):
                raise AssessmentSubmissionError("assessment new task state differs")

    def _check_policy_result(self, raw):
        a = cast(Applicant, self.applicant)
        if self.path == ("screened", "accepted", "onboarding"):
            cohort = self.db.get(Cohort, a.cohort_id)
            notes = (
                f"auto-accepted: entrance score {a.assessment_score:.0%} "
                f">= threshold {cohort.auto_accept_threshold:.0%}"
            )
            token = hash_token(raw) if type(raw) is str and raw else None
        else:
            notes = self.reason if self.path else dict(self.start)["notes"]
            token = dict(self.start)["onboarding_token_hash"]
        if (
            a.notes != notes
            or a.onboarding_token_hash != token
            or a.status != (self.path[-1] if self.path else dict(self.start)["status"])
        ):
            raise AssessmentSubmissionError("assessment persisted policy consequence differs")

    def issue(self, db, **kwargs):
        try:
            _require_assessment(self, db, self.applicant)
            index = len(self.receipts)
            if index >= len(self.path):
                raise AssessmentSubmissionError("assessment audit count differs")
            old = dict(self.start)["status"] if index == 0 else self.path[index - 1]
            details = dict(from_status=old, to_status=self.path[index], reason=self.reason, source="assessment_policy")
            if kwargs != dict(
                tenant_id=self.tenant_id,
                actor_person_id=None,
                action="applicant.transition",
                entity_type="applicant",
                entity_id=str(self.applicant_id),
                details=details,
            ):
                raise AssessmentSubmissionError("assessment audit command differs")
            if cast(Applicant, self.applicant).status != self.path[index]:
                raise AssessmentSubmissionError("assessment transition result differs")
            self._check_inputs()
            expected = dict(
                tenant_id=self.tenant_id,
                actor_type="system",
                actor_id="assessment_policy",
                actor_party_id=None,
                actor_label=None,
                source_application="dotmac_academy_app",
                action="applicant.transition",
                entity_type="applicant",
                entity_id=str(self.applicant_id),
                details=deepcopy(details),
                is_success=None,
                request_id=None,
                status_code=None,
                ip_address=None,
                user_agent=None,
            )
            row = write_audit_event(db, **expected)
            self.require()
            db.flush()
            db.refresh(row)
            self.require()
            if (
                type(row.id) is not UUID
                or row.id in self.prior_audits
                or row.id in {item[0].id for item in self.receipts}
                or any(getattr(row, key) != value for key, value in expected.items())
                or row.occurred_at is None
                or row.created_at is None
            ):
                raise AssessmentSubmissionError("assessment canonical audit receipt differs")
            self.receipts = (*self.receipts, (row, _values(row)))
        except BaseException:
            self.failed = True
            raise

    def execute(self):
        raw = None
        try:
            self._check_inputs()
            self.executing = True
            a = cast(Applicant, self.applicant)
            selected = entrance_exam.sample_for(a, self.questions, entrance_exam.questions_per_category(self.db))
            self.answers = dict(a.assessment_answers or {})
            for q in selected:
                posted = [value for value in self.form.getlist(q.ext_id) if isinstance(value, str)]
                if posted:
                    self.answers[q.ext_id] = posted
            # Preserve the sole grading owner, real paper/answers and server clock.
            self.now = datetime.now(UTC)
            try:
                result = entrance_exam.grade_and_record(
                    self.db, tenant_id=self.tenant_id, applicant=a, answers=self.answers, now=self.now
                )
            except (BadRequestError, NotFoundError):
                raise AssessmentGradeRejected() from None
            self.require()
            self.db.refresh(a)
            if (
                result
                != {
                    "score": a.assessment_score,
                    "level": a.assessment_level,
                    "profile": a.assessment_profile,
                    "valid": a.assessment_valid,
                    "invalid_reason": a.assessment_invalid_reason,
                }
                or a.assessment_taken_at != self.now
                or a.assessment_result_version != (dict(self.start)["assessment_result_version"] or 0) + 1
                or a.assessment_answers is not None
                or a.assessment_erp_synced_at is not None
            ):
                raise AssessmentSubmissionError("assessment persisted grade differs")
            grade_fields = {
                "assessment_score",
                "assessment_level",
                "assessment_profile",
                "assessment_valid",
                "assessment_invalid_reason",
                "assessment_time_exceeded",
                "assessment_taken_at",
                "assessment_result_version",
                "assessment_erp_synced_at",
                "assessment_answers",
                "assessment_elapsed_seconds",
                "assessment_bank_id",
                "updated_at",
            }
            if any(getattr(a, key) != value for key, value in self.start if key not in grade_fields):
                raise AssessmentSubmissionError("assessment grader changed unrelated applicant facts")
            self.result = _values(a)
            self._check_grade()
            self._check_inputs()
            self._derive_path()
            raw = admissions.apply_assessment_policy(self.db, applicant=a, _assessment_context=self)
            self.require()
            self._check_grade()
            if (
                len(self.receipts) != len(self.path)
                or a.status != (self.path[-1] if self.path else dict(self.start)["status"])
                or bool(raw) != (self.path == ("screened", "accepted", "onboarding"))
            ):
                raise AssessmentSubmissionError("assessment policy result differs")
            self._check_policy_result(raw)
            self._check_tasks()
            if raw:
                if a.onboarding_token_hash != hash_token(raw):
                    raise AssessmentSubmissionError("assessment onboarding token differs")
                kind, key = "onboarding_invite", f"onboarding-invite:{a.id}:{a.onboarding_token_hash}"
                queued = applicant_email.send_onboarding_invite(
                    self.db, applicant=a, url=f"{self.base_url}/onboarding?token={raw}"
                )
            elif a.status == "waitlisted":
                kind, key = "assessment_waitlist", f"assessment-waitlist:{a.id}:{a.assessment_reset_count}"
                queued = applicant_email.send_waitlist_notice(self.db, applicant=a)
            else:
                kind, key = "assessment_review", f"assessment-review:{a.id}:{a.assessment_reset_count}"
                queued = applicant_email.send_results_received(self.db, applicant=a)
            self.require()
            if type(queued) is not bool:
                raise AssessmentSubmissionError("assessment email enqueue result differs")
            self._check_grade()
            self._check_policy_result(raw)
            self.db.flush()
            rows = self._outbox()
            delivery = next((row for row in rows if row.idempotency_key == key), None)
            if a.email:
                if (
                    delivery is None
                    or (delivery.tenant_id, delivery.kind, delivery.recipient, delivery.payload)
                    != (self.tenant_id, kind, a.email, {})
                    or queued != (delivery.id not in self.prior_outbox)
                ):
                    raise AssessmentSubmissionError("assessment outcome outbox differs")
            elif queued or (delivery is not None and delivery.id not in self.prior_outbox):
                raise AssessmentSubmissionError("blank assessment email consequence differs")
            if {row.id for row in rows} != set(self.prior_outbox) | ({delivery.id} if delivery else set()):
                raise AssessmentSubmissionError("assessment created unrelated outcome email")
            for row in rows:
                if row.id in self.prior_outbox and _values(row) != self.prior_outbox[row.id]:
                    raise AssessmentSubmissionError("assessment changed existing outcome email")
            self.pins = tuple((row, _values(row)) for row in (a, *self._tasks(), *rows))
            self.audit_ids = frozenset(self._audit_rows())
            if self.audit_ids != self.prior_audits | {row.id for row, _ in self.receipts}:
                raise AssessmentSubmissionError("assessment audit inventory differs")
            self.finished = True
            self.complete()
            return AssessmentSubmission(a, "completed")
        except BaseException:
            self.failed = True
            raise
        finally:
            self.executing = False
            self.answers = None
            raw = None

    def complete(self):
        try:
            self.require()
            if not self.finished or len(self.receipts) != len(self.path):
                raise AssessmentSubmissionError("assessment operation incomplete")
            self.db.flush()
            self._check_inputs()
            for row, expected in (*self.pins, *self.receipts):
                self.db.refresh(row)
                if _values(row) != expected:
                    raise AssessmentSubmissionError("assessment persisted consequence changed")
            outbox_ids = {row.id for row, _ in self.pins if type(row) is EmailOutbox}
            task_ids = {row.id for row, _ in self.pins if type(row) is OnboardingTask}
            if (
                frozenset(self._audit_rows()) != self.audit_ids
                or {row.id for row in self._outbox()} != outbox_ids
                or {row.id for row in self._tasks()} != task_ids
            ):
                raise AssessmentSubmissionError("assessment consequence inventory changed")
            self._check_inputs()  # Include expiry/policy changes while final reads waited.
        except BaseException:
            self.failed = True
            raise


def _require_assessment(context, db, applicant):
    if type(context) is not _AssessmentContext:
        raise AssessmentSubmissionError("exact assessment producer origin required")
    try:
        context.require()
        if context.db is not db or context.applicant is not applicant or not context.executing:
            raise AssessmentSubmissionError("assessment producer binding differs")
        context._check_grade()
    except BaseException:
        context.failed = True
        raise


def _write_assessment_event(context, db, **kwargs):
    return context.issue(db, **kwargs)


@event.listens_for(Session, "before_commit")
def _assessment_before_commit(db):
    contexts = tuple(_ROOTS.get(db, ()))
    if not contexts:
        return
    state = current_conflict_savepoint(db)
    if state is not None:
        if not any(context.savepoint is state.savepoint for context in contexts) or not claim_conflict_savepoint_commit(
            db, expected_root=contexts[0].root
        ):
            for context in contexts:
                context.failed = True
            raise TransactionChanged("assessment participant attempted transaction completion")
    elif db.get_nested_transaction() is not None:
        for context in contexts:
            context.failed = True
        raise TransactionChanged("assessment unowned savepoint completion")
    for context in contexts:
        context.complete()


@event.listens_for(Session, "after_transaction_end")
def _assessment_root_ended(db, transaction):
    if transaction.parent is None:
        contexts = _ROOTS.pop(db, ())
        for context in contexts:
            if context.executing:
                context.ended = context.failed = True
            else:
                context.__dict__.clear()


async def submit_applicant_assessment(db, *, request):
    """Execute the actual form submit; never accept supplied actor/result proof."""
    context = form = raw = root = None
    if not isinstance(request, Request) or not isinstance(db, Session):
        raise AssessmentSubmissionError("actual Request and Session required")
    form = await request.form()
    raw = str(form.get("token") or "")
    digest: str | None = hash_token(raw)
    raw = None
    try:
        with conflict_savepoint(db):
            tenant_id = require_tenant(request).id
            if not digest or not form.get("token"):
                return AssessmentSubmission(None, "already_completed")
            context = _AssessmentContext(db, request, tenant_id, digest, form)
            root = context._root
            _ROOTS.setdefault(db, []).append(context)
            context.require()
            disposition = context.prove()
            if disposition:
                _ROOTS[db].remove(context)
                context.__dict__.clear()
                context = None
                return AssessmentSubmission(None, disposition)
            return context.execute()
    except BaseException as exc:
        if context is not None:
            context.failed = True
            # Successful helper rollback removed all operation effects. Release
            # its guard so the caller may still commit its earlier flushed work.
            if (
                db.get_transaction() is root
                and db.is_active
                and not conflict_savepoint_requires_rollback(db, expected_root=cast(SessionTransaction, root))
            ):
                _ROOTS[db].remove(context)
                if not _ROOTS[db]:
                    del _ROOTS[db]
                context.__dict__.clear()
            elif context.ended:
                context.__dict__.clear()
        # The helper preserves the exact cancellation and typed transaction
        # failure. Its own rollback isolates operation DML from prior work.
        if not isinstance(exc, Exception) or isinstance(
            exc,
            (TransactionChanged, BadRequestError, NotFoundError, AssessmentSubmissionError, AssessmentGradeRejected),
        ):
            raise
        raise AssessmentSubmissionError("assessment submit failed; no operation change was saved") from None
    finally:
        raw = digest = form = request = None
