"""Management-course public inquiry notifications."""

# Ported at new-composition genesis (presentation-coupled wave).

from __future__ import annotations

import html
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.services.admin_reports import admin_recipients
from app.services.email_outbox import enqueue_email
from app.services.settings_store import effective


def _clean(value: str, limit: int) -> str:
    return " ".join((value or "").strip().split())[:limit]


def _paragraph(value: str, limit: int) -> str:
    return (value or "").strip()[:limit]


def queue_management_inquiry(
    db: Session,
    *,
    tenant_id: UUID,
    first_name: str,
    last_name: str,
    email: str,
    phone: str = "",
    learner_type: str = "",
    course_interest: str = "",
    message: str = "",
) -> int:
    """Queue a public management-course inquiry for Academy admins.

    The inquiry is intentionally email-only for now: management enrollment is
    arranged by contact, and the existing outbox already gives durable retry and
    delivery evidence without adding a new public-leads table.
    """
    data = {
        "first_name": _clean(first_name, 80),
        "last_name": _clean(last_name, 80),
        "email": _clean(email, 254).lower(),
        "phone": _clean(phone, 40),
        "learner_type": _clean(learner_type, 40),
        "course_interest": _clean(course_interest, 160),
        "message": _paragraph(message, 1000),
    }
    recipients = [person.email for person in admin_recipients(db, tenant_id=tenant_id)]
    fallback = _clean(str(effective(db).management_inquiry_recipient), 254)
    if not recipients and fallback:
        recipients = [fallback]

    name = f"{data['first_name']} {data['last_name']}".strip() or "Management-course inquiry"
    rows = [
        ("Name", name),
        ("Email", data["email"]),
        ("Phone", data["phone"] or "-"),
        ("For", data["learner_type"] or "-"),
        ("Course interest", data["course_interest"] or "-"),
        ("Message", data["message"] or "-"),
    ]
    html_rows = "".join(
        "<tr>"
        f"<td style='padding:6px 14px 6px 0;color:#5B6B62;'>{html.escape(label)}</td>"
        f"<td style='padding:6px 0;font-weight:600;'>{html.escape(value)}</td>"
        "</tr>"
        for label, value in rows
    )
    html_body = (
        "<div style=\"font-family:system-ui,-apple-system,'Segoe UI',sans-serif;color:#0D1F16;"
        'max-width:560px;margin:0 auto;padding:24px;">'
        "<p style='font-size:12px;letter-spacing:.14em;text-transform:uppercase;"
        "color:#0B4F31;font-weight:600;margin:0 0 4px;'>Dotmac Academy</p>"
        "<h1 style='font-size:22px;margin:0 0 16px;'>Management-course inquiry</h1>"
        f"<table style='border-collapse:collapse;font-size:14px;'>{html_rows}</table>"
        "</div>"
    )
    text_body = "Dotmac Academy management-course inquiry\n\n" + "\n".join(f"{label}: {value}" for label, value in rows)

    sent = 0
    inquiry_id = uuid4()
    for recipient in dict.fromkeys(recipients):
        if enqueue_email(
            db,
            tenant_id=tenant_id,
            idempotency_key=f"management-inquiry:{inquiry_id}:{recipient}",
            kind="management_inquiry",
            recipient=recipient,
            subject=f"Management-course inquiry from {name}",
            html_body=html_body,
            text_body=text_body,
            payload=data,
        ):
            sent += 1
    return sent
