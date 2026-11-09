"""Project staff course progress to dotmac_erp HR.

The hourly state-derived sweep sends changed percentages using the stable ERP
employee reference carried by a staff enrolment. Accepted snapshots advance
the persisted progress marker so unchanged state is not resent. Completed-course
delivery acknowledgement lives in Academy's ERP checkpoint, not learning state.
"""

# Ported at new-composition genesis (ERP slice).

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import UTC, datetime
from uuid import UUID

import httpx
from dotmac_learning.models import Course, CourseCompletion
from dotmac_learning.participation_models import CourseOffering, Enrollment
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.config import settings
from app.models.erp_completion_checkpoint import ERPCompletionCheckpoint, ERPImportedCompletionKey

logger = logging.getLogger(__name__)

# Payload contract version. ERP dispatches on it, so a future breaking change
# ships as version 2 alongside the old handler rather than as a silent reshape.
CONTRACT_VERSION = 2

# Push outcomes. UNMATCHED is deliberately not FAILED: retrying will not fix an
# identity mismatch, but it must not be mistaken for delivery either.
SYNCED = "synced"
UNMATCHED = "unmatched"
FAILED = "failed"


def _sign(body: bytes) -> str:
    digest = hmac.new(settings.erp_webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def build_payload(*, enrollment: Enrollment, course: Course, completion: CourseCompletion) -> dict:
    """Build the stable-identity v2 training projection ERP expects."""
    completed = completion.status == "completed"
    payload = {
        "version": CONTRACT_VERSION,
        "event": "course_completed" if completed else "training_progress_updated",
        "employee_ref": enrollment.employee_ref,
        "academy_enrollment_ref": str(enrollment.id),
        "academy_course_ref": course.source_ref,
        "course_title": course.title,
        "course_version": course.version,
        "progress_pct": round(completion.pct * 100, 2),
        "status": completion.status,
        "occurred_at": completion.updated_at.isoformat(),
    }
    if completed:
        payload.update(
            passed=True,
            completed_on=(completion.completed_at.date().isoformat() if completion.completed_at else None),
            certificate_ref=str(completion.id),
        )
    return payload


# Reply bodies meaning "ERP understood us and wrote nothing".
_NOT_RECORDED = frozenset({"ignored", "unsupported"})
_RECORDED = frozenset({"recorded", "updated", "duplicate"})


def _reply_status(resp: httpx.Response) -> str | None:
    """The ``status`` field of an ERP reply, or None if unreadable.

    ERP returns this field on both success and refusal, at the top level of a
    2xx body and under ``detail`` in a 4xx error body.
    """
    try:
        body = resp.json()
    except Exception:
        return None
    if not isinstance(body, dict):
        return None
    detail = body.get("detail")
    if isinstance(detail, dict) and "status" in detail:
        body = detail
    status = body.get("status")
    return str(status).lower() if status is not None else None


def _outcome(resp: httpx.Response) -> str:
    """Classify an ERP reply into SYNCED / UNMATCHED / FAILED.

    The body is read before the status line, deliberately. ERP historically
    answered an unmatched employee with HTTP 200 and ``{"status": "ignored"}``,
    and now answers 422 with the same status in ``detail`` — both mean the same
    thing, and both must classify as UNMATCHED rather than one being mistaken
    for delivery and the other for a transient failure worth retrying forever.
    """
    status = _reply_status(resp)
    if status in _NOT_RECORDED:
        return UNMATCHED
    if resp.status_code // 100 != 2:
        return FAILED
    if status not in _RECORDED:
        # A 2xx without an explicit acknowledgement is not evidence of a
        # durable write. Retry rather than silently discard the event.
        return FAILED
    return SYNCED


class ERPCheckpointInvariantError(RuntimeError):
    """Delivery history cannot safely be inferred from an absent checkpoint."""


def _locked_checkpoint(
    db: Session,
    *,
    tenant_id: UUID,
    completion: CourseCompletion,
) -> ERPCompletionCheckpoint:
    if completion.tenant_id != tenant_id:
        raise ERPCheckpointInvariantError("completion belongs to another tenant")
    checkpoint = db.scalars(
        select(ERPCompletionCheckpoint)
        .where(ERPCompletionCheckpoint.tenant_id == tenant_id)
        .where(ERPCompletionCheckpoint.completion_id == completion.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    if checkpoint is None and db.get(ERPImportedCompletionKey, (tenant_id, completion.id)) is not None:
        raise ERPCheckpointInvariantError("missing checkpoint for imported completion")
    # Concurrent first senders converge on one key; INSERT waits for the other
    # transaction before the lock/read. The caller owns commit and lock release.
    db.execute(
        insert(ERPCompletionCheckpoint)
        .values(tenant_id=tenant_id, completion_id=completion.id)
        .on_conflict_do_nothing(index_elements=["tenant_id", "completion_id"])
    )
    return db.scalars(
        select(ERPCompletionCheckpoint)
        .where(ERPCompletionCheckpoint.tenant_id == tenant_id)
        .where(ERPCompletionCheckpoint.completion_id == completion.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()


def _eligible_enrollments(db: Session, *, tenant_id: UUID, completion: CourseCompletion) -> list[Enrollment]:
    return list(
        db.scalars(
            select(Enrollment)
            .join(
                CourseOffering,
                (CourseOffering.tenant_id == Enrollment.tenant_id) & (CourseOffering.cohort_id == Enrollment.cohort_id),
            )
            .where(
                Enrollment.tenant_id == tenant_id,
                Enrollment.person_id == completion.person_id,
                Enrollment.status == "active",
                Enrollment.audience == "staff",
                Enrollment.employee_ref.is_not(None),
                func.length(func.trim(Enrollment.employee_ref)) > 0,
                CourseOffering.tenant_id == tenant_id,
                CourseOffering.course_id == completion.course_id,
                CourseOffering.status == "active",
            )
            .execution_options(populate_existing=True)
        )
        .unique()
        .all()
    )


def push_completion(
    db: Session,
    *,
    tenant_id: UUID,
    completion: CourseCompletion,
    enrollment: Enrollment | None = None,
    now: datetime | None = None,
) -> str:
    """Push one completion to ERP. Returns SYNCED / UNMATCHED / FAILED.

    Checkpoint ``erp_synced_at`` is stamped only on SYNCED — an event ERP declined to
    record is not delivered, and leaving it unstamped keeps it in the backlog
    where it is visible and retried. Network errors return FAILED; broken
    checkpoint invariants raise ERPCheckpointInvariantError. Caller owns commit.
    """
    if not settings.erp_webhook_url:
        return FAILED
    if enrollment is not None and (enrollment.tenant_id != tenant_id or enrollment.person_id != completion.person_id):
        raise ERPCheckpointInvariantError("supplied enrollment identity mismatch")
    supplied_enrollment_id = None if enrollment is None else enrollment.id
    checkpoint = _locked_checkpoint(db, tenant_id=tenant_id, completion=completion)
    # The sweep may have waited behind another sender. Re-read canonical inputs
    # under the product lock without taking a Learning write lock over HTTP.
    current = db.scalars(
        select(CourseCompletion)
        .where(CourseCompletion.tenant_id == tenant_id, CourseCompletion.id == completion.id)
        .execution_options(populate_existing=True)
    ).one_or_none()
    if current is None:
        raise ERPCheckpointInvariantError("completion disappeared while acquiring checkpoint")
    completion = current
    enrollments = _eligible_enrollments(db, tenant_id=tenant_id, completion=completion)
    if len(enrollments) > 1:
        raise ERPCheckpointInvariantError("conflicting eligible ERP enrollment identities")
    if not enrollments or (supplied_enrollment_id is not None and enrollments[0].id != supplied_enrollment_id):
        return FAILED
    enrollment = enrollments[0]
    course = db.scalars(
        select(Course)
        .where(Course.tenant_id == tenant_id, Course.id == completion.course_id)
        .execution_options(populate_existing=True)
    ).one_or_none()
    if course is None:
        return FAILED
    if checkpoint.erp_synced_pct == completion.pct:
        return SYNCED
    if completion.status == "completed" and checkpoint.erp_synced_at is not None:
        # Preserve the imported raw ACK; this is suppression, not a new receipt.
        checkpoint.erp_synced_pct = completion.pct
        db.flush()
        return SYNCED

    payload = build_payload(enrollment=enrollment, course=course, completion=completion)
    sent_pct = completion.pct
    sent_completed = completion.status == "completed"
    body = json.dumps(payload).encode()
    try:
        resp = httpx.post(
            settings.erp_webhook_url,
            content=body,
            headers={"Content-Type": "application/json", "X-Webhook-Signature-256": _sign(body)},
            timeout=15.0,
        )
    except Exception as exc:  # network / timeout — leave unsynced, retry next sweep
        logger.warning("erp training push failed for completion %s: %s", completion.id, exc)
        return FAILED

    outcome = _outcome(resp)
    if outcome == UNMATCHED:
        logger.warning(
            "erp training push not recorded for completion %s: ERP matched no employee for %s",
            completion.id,
            enrollment.employee_ref,
        )
        return UNMATCHED
    if outcome == FAILED:
        logger.warning("erp training push rejected (%s) for completion %s", resp.status_code, completion.id)
        return FAILED

    checkpoint.erp_synced_pct = sent_pct
    if sent_completed:
        checkpoint.erp_synced_at = now or datetime.now(UTC)
    db.flush()
    if sent_completed:
        # App-sync pilot shadow: mirror the delivered completion. The
        # helper never raises and its outcome never alters SYNCED.
        from app.services import erp_app_sync

        erp_app_sync.push_shadow_completion(enrollment=enrollment, course=course, completion=completion)
    return SYNCED


def sync_pending(db: Session, *, tenant_id: UUID, now: datetime | None = None) -> dict[str, int]:
    """Push every completed, not-yet-synced completion. Returns per-outcome counts.

    Counts are the point: a silent zero and a silent hundred-unmatched used to
    look identical from the outside.
    """
    counts = {SYNCED: 0, UNMATCHED: 0, FAILED: 0}
    if not settings.erp_webhook_url:
        return counts
    rows = db.execute(
        select(CourseCompletion, Enrollment)
        .join(
            CourseOffering,
            (CourseOffering.tenant_id == CourseCompletion.tenant_id)
            & (CourseOffering.course_id == CourseCompletion.course_id),
        )
        .join(
            Enrollment,
            (Enrollment.tenant_id == CourseOffering.tenant_id)
            & (Enrollment.cohort_id == CourseOffering.cohort_id)
            & (Enrollment.person_id == CourseCompletion.person_id),
        )
        .outerjoin(
            ERPCompletionCheckpoint,
            (ERPCompletionCheckpoint.tenant_id == CourseCompletion.tenant_id)
            & (ERPCompletionCheckpoint.completion_id == CourseCompletion.id),
        )
        .where(
            CourseCompletion.tenant_id == tenant_id,
            Enrollment.audience == "staff",
            Enrollment.employee_ref.is_not(None),
            func.length(func.trim(Enrollment.employee_ref)) > 0,
            Enrollment.status == "active",
            CourseOffering.status == "active",
            ERPCompletionCheckpoint.erp_synced_pct.is_distinct_from(CourseCompletion.pct),
        )
    ).all()
    seen: set[UUID] = set()
    for completion, enrollment in rows:
        if completion.id in seen:
            continue
        seen.add(completion.id)
        outcome = push_completion(
            db,
            tenant_id=tenant_id,
            completion=completion,
            enrollment=enrollment,
            now=now,
        )
        counts[outcome] += 1
    from app.services import erp_assessment_sync

    for outcome, amount in erp_assessment_sync.sync_pending(db, tenant_id=tenant_id, now=now).items():
        counts[outcome] += amount
    return counts
