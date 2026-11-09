"""Materialize committed lab consequences without SMTP or transaction ownership."""

# Ported at new-composition genesis (presentation-coupled wave).

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from dotmac_learning import CertificateIssue, CompletionResult, get_activity, get_certificate, get_completion
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.lab_result_delivery import LabResultDelivery
from app.services.completion import _notification_recipient, deliver_completion_notifications
from app.services.email import notify_score_if_first_pass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AcademyDeliveryScore:
    """Event-time score fields needed by Academy's shared delivery formatter."""

    id: UUID
    tenant_id: UUID
    score: float
    max_score: float
    fraction: float
    passed: bool


@dataclass(frozen=True)
class LabDeliveryCounts:
    delivered: int
    retried: int
    failed_total: int
    oldest_pending_age_s: float


def _materialize_one(db: Session, intent: LabResultDelivery) -> None:
    keys = dict(tenant_id=intent.tenant_id, person_id=intent.person_id, course_id=intent.course_id)
    if intent.first_completion:
        completion = get_completion(db, **keys)
        if completion is None:
            raise RuntimeError("completion unavailable")
        certificate = None
        if intent.certificate_id is not None:
            fact = get_certificate(db, **keys)
            if fact is None or fact.id != intent.certificate_id:
                raise RuntimeError("certificate unavailable")
            certificate = CertificateIssue(certificate=fact, created=intent.certificate_created)
        elif intent.certificate_created:
            raise RuntimeError("certificate unavailable")
        deliver_completion_notifications(
            db,
            CompletionResult(completion=completion, first_completion=intent.first_completion, certificate=certificate),
            raise_on_failure=True,
        )
    if intent.first_pass and intent.passed:
        person = _notification_recipient(db, tenant_id=intent.tenant_id, person_id=intent.person_id)
        activity = get_activity(db, tenant_id=intent.tenant_id, activity_id=intent.activity_id)
        if activity is None:
            raise RuntimeError("activity unavailable")
        notify_score_if_first_pass(
            db,
            score=AcademyDeliveryScore(
                id=intent.score_id,
                tenant_id=intent.tenant_id,
                score=intent.score,
                max_score=intent.max_score,
                fraction=intent.fraction,
                passed=intent.passed,
            ),
            activity=activity,
            person=person,
            first_pass=intent.first_pass,
            raise_on_failure=True,
        )


def materialize_pending(
    db: Session, *, limit: int = 100, now: datetime | None = None, max_attempts: int = 8
) -> LabDeliveryCounts:
    """Lock due intents; atomically save effects and success within each savepoint.

    The caller must commit before SMTP delivery. Failed savepoints leave only
    retry metadata in the outer transaction; exception payloads never enter it.
    """
    now = now or datetime.now(UTC)
    rows = db.scalars(
        select(LabResultDelivery)
        .where(LabResultDelivery.status == "pending", LabResultDelivery.next_attempt_at <= now)
        .order_by(LabResultDelivery.next_attempt_at, LabResultDelivery.created_at)
        .limit(max(0, limit))
        .with_for_update(skip_locked=True)
    ).all()
    delivered = retried = 0
    for intent in rows:
        try:
            with db.begin_nested():
                _materialize_one(db, intent)
                intent.status = "delivered"
                intent.delivered_at = now
                intent.last_error = None
                db.flush()
            delivered += 1
        except Exception as exc:
            intent.attempts += 1
            category = getattr(exc, "_academy_delivery_category", "materialize")
            if category not in {"notify", "enqueue", "materialize"}:
                category = "materialize"
            intent.last_error = f"{type(exc).__name__}:{category}"[:120]
            intent.next_attempt_at = now + timedelta(minutes=min(2 ** min(intent.attempts, 6), 60))
            intent.status = "failed" if intent.attempts >= max_attempts else "pending"
            retried += intent.status == "pending"
            logger.warning(
                "lab delivery failed: %s tenant=%s operation=%s",
                intent.last_error,
                intent.tenant_id,
                intent.operation_id,
            )
    db.flush()
    failed_total = db.scalar(
        select(func.count()).select_from(LabResultDelivery).where(LabResultDelivery.status == "failed")
    )
    oldest = db.scalar(select(func.min(LabResultDelivery.created_at)).where(LabResultDelivery.status == "pending"))
    age = max(0.0, (now - oldest).total_seconds()) if oldest else 0.0
    return LabDeliveryCounts(delivered, retried, failed_total or 0, age)


def requeue_failed(db: Session, *, tenant_id: UUID | None = None, now: datetime | None = None, limit: int = 100) -> int:
    query = select(LabResultDelivery).where(LabResultDelivery.status == "failed")
    if tenant_id is not None:
        query = query.where(LabResultDelivery.tenant_id == tenant_id)
    rows = db.scalars(
        query.order_by(LabResultDelivery.created_at).limit(max(0, limit)).with_for_update(skip_locked=True)
    ).all()
    for row in rows:
        row.status, row.attempts, row.last_error = "pending", 0, None
        row.next_attempt_at = now or datetime.now(UTC)
    db.flush()
    return len(rows)


def purge_delivered(db: Session, *, now: datetime | None = None, retention_days: int = 90, limit: int = 100) -> int:
    cutoff = (now or datetime.now(UTC)) - timedelta(days=retention_days)
    rows = db.scalars(
        select(LabResultDelivery)
        .where(LabResultDelivery.status == "delivered", LabResultDelivery.delivered_at < cutoff)
        .order_by(LabResultDelivery.delivered_at)
        .limit(max(0, limit))
        .with_for_update(skip_locked=True)
    ).all()
    for row in rows:
        db.delete(row)
    db.flush()
    return len(rows)
