"""Transactional email intent, idempotent delivery, and retry reconciliation."""

# Ported at new-composition genesis (delivery-core slice).

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models.email_outbox import EmailOutbox
from app.services.email import EmailResult, send_email_detailed

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 8


def enqueue_email(
    db: Session,
    *,
    tenant_id: UUID,
    idempotency_key: str,
    kind: str,
    recipient: str | None,
    subject: str,
    html_body: str,
    text_body: str | None = None,
    payload: dict | None = None,
) -> bool:
    """Persist one consequence in the caller's transaction.

    PostgreSQL's conflict handling makes retries/concurrent callers idempotent
    without rolling back the surrounding domain transaction.

    Returns True when THIS call created the row. A duplicate returns False: the
    consequence is still safely queued (the earlier row carries it), but the
    caller has not caused a new send. Callers count sends with this, so the
    previous unconditional True made every "queued" figure a count of attempts
    — indistinguishable from a job that genuinely re-sent to everyone.
    """
    if not recipient:
        return False
    inserted = db.execute(
        insert(EmailOutbox)
        .values(
            id=uuid4(),
            tenant_id=tenant_id,
            idempotency_key=idempotency_key[:200],
            kind=kind[:60],
            recipient=recipient,
            subject=subject,
            html_body=html_body,
            text_body=text_body,
            payload=payload or {},
        )
        .on_conflict_do_nothing(index_elements=[EmailOutbox.tenant_id, EmailOutbox.idempotency_key])
        .returning(EmailOutbox.id)
    ).first()
    db.flush()
    return inserted is not None


def deliver_pending(db: Session, *, limit: int = 100, now: datetime | None = None) -> dict[str, int]:
    """Deliver due rows, persisting attempts and retry schedules.

    The worker holds a row lock during SMTP delivery. A crash rolls the attempt
    back to pending; a stable Message-ID lets downstream mail systems deduplicate
    that narrow crash window.
    """
    now = now or datetime.now(UTC)
    counts = {"sent": 0, "retried": 0, "failed": 0}
    for _ in range(limit):
        row = db.scalars(
            select(EmailOutbox)
            .where(EmailOutbox.status == "pending")
            .where(EmailOutbox.available_at <= now)
            .order_by(EmailOutbox.available_at, EmailOutbox.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        ).first()
        if row is None:
            break

        try:
            attachments = _attachments(db, row)
            result = send_email_detailed(
                row.recipient,
                row.subject,
                row.html_body,
                text_body=row.text_body,
                db=db,
                attachments=attachments,
                message_id=f"<academy-outbox-{row.id}@dotmac.local>",
            )
        except Exception as exc:
            error_class = type(exc).__name__
            logger.warning(
                "could not prepare email outbox row %s (%s)",
                row.id,
                error_class,
            )
            result = EmailResult(False, error_class)
        row.attempts += 1
        if result.sent:
            row.status = "sent"
            row.sent_at = now
            row.last_error = None
            _apply_delivery_projection(db, row, now)
            counts["sent"] += 1
        elif row.attempts >= MAX_ATTEMPTS:
            row.status = "failed"
            row.last_error = result.error[:120] or "delivery_failed"
            counts["failed"] += 1
        else:
            delay_minutes = min(24 * 60, 2 ** min(row.attempts, 10))
            row.available_at = now + timedelta(minutes=delay_minutes)
            row.last_error = result.error[:120] or "delivery_failed"
            counts["retried"] += 1
        db.flush()
    return counts


def requeue_failed(db: Session, *, tenant_id: UUID | None = None) -> int:
    """Idempotent reconciler: put terminal failures back into the delivery queue."""
    stmt = select(EmailOutbox).where(EmailOutbox.status == "failed").with_for_update()
    if tenant_id is not None:
        stmt = stmt.where(EmailOutbox.tenant_id == tenant_id)
    rows = list(db.scalars(stmt).all())
    now = datetime.now(UTC)
    for row in rows:
        row.status = "pending"
        row.attempts = 0
        row.available_at = now
        row.last_error = None
    db.flush()
    return len(rows)


def _attachments(db: Session, row: EmailOutbox) -> list[tuple[str, bytes, str]]:
    if row.kind == "weekly_digest":
        try:
            cohort_id = UUID(str(row.payload["cohort_id"]))
        except (KeyError, ValueError):
            return []  # pre-P3b digest rows carry no cohort payload
        from app.services.csv_reports import weekly_report_attachments

        return weekly_report_attachments(db, tenant_id=row.tenant_id, cohort_id=cohort_id)
    if row.kind != "certificate":
        return []
    try:
        person_id = UUID(str(row.payload["person_id"]))
        course_id = UUID(str(row.payload["course_id"]))
    except (KeyError, ValueError) as exc:
        raise ValueError("invalid certificate attachment payload") from exc

    from dotmac_learning import get_certificate, get_course

    from app.services.api_identity_reads import certificate_recipient
    from app.services.certificates import render_certificate_pdf

    person = certificate_recipient(db, tenant_id=row.tenant_id, person_id=person_id)
    course = get_course(db, tenant_id=row.tenant_id, course_id=course_id)
    cert = get_certificate(db, tenant_id=row.tenant_id, person_id=person_id, course_id=course_id)
    if person is None or course is None or cert is None:
        raise ValueError("certificate attachment state is missing")
    pdf = render_certificate_pdf(
        recipient_name=f"{person.first_name} {person.last_name}".strip(),
        course_title=course.title,
        serial=cert.serial,
        issued_at=cert.issued_at,
    )
    return [("certificate.pdf", pdf, "application/pdf")]


def _apply_delivery_projection(db: Session, row: EmailOutbox, sent_at: datetime) -> None:
    """Project transport success onto optional domain-facing delivery metadata."""
    if row.kind != "entrance_invite":
        return
    try:
        applicant_id = UUID(str(row.payload["applicant_id"]))
    except (KeyError, ValueError):
        return
    from app.models.admissions import Applicant

    applicant = db.scalars(
        select(Applicant).where(Applicant.tenant_id == row.tenant_id).where(Applicant.id == applicant_id)
    ).first()
    if applicant is not None:
        applicant.invite_sent_at = sent_at
