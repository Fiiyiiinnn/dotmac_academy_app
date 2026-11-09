# Ported at new-composition genesis (learning-facing wave 2).
# app/services/completion.py
"""Learning completion delegation and Academy completion delivery."""

from __future__ import annotations

import html
import logging
from datetime import datetime
from uuid import UUID

from dotmac_kernel.models import Party, PartyPerson, PartyType
from dotmac_learning import (
    CertificateFact,
    CompletionCommand,
    CompletionFact,
    CompletionResult,
    get_course,
)
from dotmac_learning import (
    recompute_completion as learning_recompute_completion,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.identity_extensions import AcademyAccountProfile

logger = logging.getLogger(__name__)


def recompute_completion(
    db: Session,
    *,
    tenant_id: UUID,
    person_id: UUID,
    course_id: UUID,
    now: datetime | None = None,
) -> CompletionFact:
    """Recompute in the caller's transaction and deliver product effects."""
    result = learning_recompute_completion(
        db,
        CompletionCommand(tenant_id=tenant_id, person_id=person_id, course_id=course_id, now=now),
    )
    if result.first_completion:
        # Facilitator eligibility is a consequence of this canonical completion
        # fact. Reconcile in the same transaction so qualified nominees do not
        # wait for the scheduled repair sweep before receiving scoped access.
        from app.services.facilitator_nominations import (
            ReconcileCompletedCourseCommand,
            reconcile_completed_course,
        )

        reconcile_completed_course(
            db,
            command=ReconcileCompletedCourseCommand(
                tenant_id=tenant_id,
                person_id=person_id,
                course_id=course_id,
            ),
        )
    deliver_completion_notifications(db, result)
    return result.completion


def deliver_completion_notifications(db: Session, result: CompletionResult, *, raise_on_failure: bool = False) -> None:
    """Deliver Academy effects from a completed Learning command result.

    Result adapters can share this path without a second recomputation. Required
    completion and certificate writes have already succeeded in the caller's
    transaction; optional delivery retains its existing savepoint boundaries.
    """
    if not result.first_completion:
        return
    # A course with certificate_auto_issue=False completes without a
    # certificate (HM-04): notify the completion, skip certificate delivery.
    certificate = result.certificate
    completion = result.completion
    tenant_id = completion.tenant_id
    person_id = completion.person_id
    course_id = completion.course_id
    course = get_course(db, tenant_id=tenant_id, course_id=course_id)
    course_title = course.title if course is not None else "the course"
    if certificate is not None:
        from app.services.certificates import deliver_certificate_notification

        deliver_certificate_notification(db, certificate, raise_on_failure=raise_on_failure)
    try:
        from app.services.notifications import notify

        with db.begin_nested():
            notify(
                db,
                tenant_id=tenant_id,
                person_id=person_id,
                kind="course_completed",
                title="Well done, course completed",
                body=f"Congratulations on completing {course_title}.",
                link=f"/certificates/{course_id}" if certificate is not None else "/progress",
            )
    except Exception as exc:
        if raise_on_failure:
            exc.__dict__["_academy_delivery_category"] = "notify"
            raise
        logger.warning("course completion notification failed: %s", exc)
    if certificate is None:
        return
    try:
        with db.begin_nested():
            _email_certificate(
                db,
                tenant_id=tenant_id,
                person_id=person_id,
                course_id=course_id,
                course_title=course_title,
                certificate=certificate.certificate,
            )
    except Exception as exc:
        if raise_on_failure:
            exc.__dict__["_academy_delivery_category"] = "enqueue"
            raise
        logger.warning("course completion certificate email failed: %s", exc)


def _notification_recipient(db: Session, *, tenant_id: UUID, person_id: UUID):
    """Read current canonical delivery fields, without account authorization.

    Scalar rows avoid stale ORM identity-map values. The subtype has no tenant
    column: Party supplies isolation and the Academy profile join supplies the
    recipient's own preferences. Missing canonical parts refuse delivery.
    Inactive/suspended accounts retain the existing notification behavior;
    this read grants no Learning or account authority and takes no row locks.
    """
    return db.execute(
        select(Party.id, Party.email, PartyPerson.first_name, AcademyAccountProfile.prefs)
        .join(PartyPerson, PartyPerson.party_id == Party.id)
        .join(
            AcademyAccountProfile,
            (AcademyAccountProfile.party_id == Party.id) & (AcademyAccountProfile.tenant_id == Party.tenant_id),
        )
        .where(Party.tenant_id == tenant_id, Party.id == person_id, Party.party_type == PartyType.person)
    ).first()


def _email_certificate(
    db: Session,
    *,
    tenant_id: UUID,
    person_id: UUID,
    course_id: UUID,
    course_title: str,
    certificate: CertificateFact,
) -> bool:
    """Queue the freshly-completed course's certificate email.

    The certificate and outbox intent commit atomically. The delivery worker
    renders the PDF after commit and retries SMTP independently.
    """
    from app.services.email import recipient_allows
    from app.services.email_outbox import enqueue_email

    person = _notification_recipient(db, tenant_id=tenant_id, person_id=person_id)
    if person is None or not person.email:
        return False
    if not recipient_allows(person, "email_results"):
        return False

    cert = certificate
    name_text = person.first_name or "there"
    name = html.escape(name_text)
    safe_title = html.escape(course_title)
    html_body = (
        "<div style=\"font-family:system-ui,-apple-system,'Segoe UI',sans-serif;color:#0D1F16;"
        'max-width:560px;margin:0 auto;padding:24px;">'
        "<p style='font-size:12px;letter-spacing:.14em;text-transform:uppercase;"
        "color:#0B4F31;font-weight:600;margin:0 0 4px;'>Dotmac Academy</p>"
        f"<h1 style='font-size:22px;margin:0 0 16px;'>Congratulations, {name}!</h1>"
        f"<p>You've completed <strong>{safe_title}</strong>. Your certificate is attached.</p>"
        f"<p style='font-size:13px;color:#5B6B62;'>Certificate serial: {cert.serial}. You can also "
        "download it any time from your course page.</p></div>"
    )
    text = (
        f"Congratulations, {name_text}!\n\nYou've completed {course_title}. "
        f"Your certificate is attached (serial {cert.serial}).\n"
    )
    return enqueue_email(
        db,
        tenant_id=tenant_id,
        idempotency_key=f"certificate:{cert.id}",
        kind="certificate",
        recipient=person.email,
        subject=f"Your certificate — {course_title}",
        html_body=html_body,
        text_body=text,
        payload={"person_id": str(person_id), "course_id": str(course_id)},
    )
