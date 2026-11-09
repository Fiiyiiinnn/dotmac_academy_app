"""Staff-only training roll-up for HR (ADR 0004).

Deliberately not a filtered copy of the admin activity report. HR has a
different question — *are our people doing the training we put them on?* — and a
different action: talk to a manager. So this reports activation and stalling by
person, not admissions throughput.

It counts only enrolments **explicitly marked staff**. An unclassified learner
is never reported to HR as staff, because we do not know that they are; the
unclassified count is reported instead, since a roll-up that quietly omits half
the roster is worse than one that admits the gap.
"""

# Ported at new-composition genesis (learning-facing wave 2).

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from html import escape
from uuid import UUID

from dotmac_kernel.models import AuthSession, Party, PartyPerson, PartyType, UserCredential
from dotmac_learning.models import CourseCompletion, LearningEvent
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.services import audience as audience_service
from app.services.email_outbox import enqueue_email

# Named individually rather than counted in aggregate — HR acts on people.
MAX_NAMED = 15


def _names(db: Session, tenant_id: UUID, person_ids: list[UUID]) -> list[str]:
    if not person_ids:
        return []
    rows = db.execute(
        select(PartyPerson.first_name, PartyPerson.last_name, Party.email)
        .select_from(Party)
        .join(PartyPerson, PartyPerson.party_id == Party.id)
        .where(Party.tenant_id == tenant_id)
        .where(Party.party_type == PartyType.person)
        .where(Party.id.in_(person_ids))
        .order_by(Party.email)
    ).all()
    return [f"{(f or '').strip()} {(last or '').strip()}".strip() or email for f, last, email in rows]


def snapshot(db: Session, *, tenant_id: UUID, since: datetime, now: datetime | None = None) -> dict:
    """Staff training state: enrolled, never activated, stalled, active, completed."""
    now = now or datetime.now(UTC)
    staff_ids = audience_service.staff_person_ids(db, tenant_id=tenant_id)
    split = audience_service.counts_by_audience(db, tenant_id=tenant_id)

    if not staff_ids:
        return {
            "since": since,
            "staff": 0,
            "never_activated": [],
            "stalled": [],
            "active_in_window": 0,
            "completed_in_window": 0,
            "unclassified": split["unclassified"],
        }

    # Never activated: no credential and no session ever — they cannot log in,
    # so no amount of manager encouragement will help until that is fixed.
    never_activated = [
        pid
        for pid in staff_ids
        if not db.scalar(
            select(func.count())
            .select_from(UserCredential)
            .where(UserCredential.tenant_id == tenant_id)
            .where(UserCredential.party_id == pid)
        )
        and not db.scalar(
            select(func.count())
            .select_from(AuthSession)
            .where(AuthSession.tenant_id == tenant_id)
            .where(AuthSession.party_id == pid)
        )
    ]

    active_ids = set(
        db.scalars(
            select(LearningEvent.person_id)
            .where(LearningEvent.tenant_id == tenant_id)
            .where(LearningEvent.person_id.in_(staff_ids))
            .where(LearningEvent.occurred_at >= since)
            .distinct()
        ).all()
    )
    # Stalled: can log in, has done something at some point, but nothing in the
    # window. Distinct from never-activated because the remedy is different.
    ever_active = set(
        db.scalars(
            select(LearningEvent.person_id)
            .where(LearningEvent.tenant_id == tenant_id)
            .where(LearningEvent.person_id.in_(staff_ids))
            .distinct()
        ).all()
    )
    stalled = [pid for pid in ever_active - active_ids if pid not in set(never_activated)]

    completed = int(
        db.scalar(
            select(func.count())
            .select_from(CourseCompletion)
            .where(CourseCompletion.tenant_id == tenant_id)
            .where(CourseCompletion.person_id.in_(staff_ids))
            .where(CourseCompletion.completed_at >= since)
        )
        or 0
    )

    return {
        "since": since,
        "staff": len(staff_ids),
        "never_activated": _names(db, tenant_id, never_activated[:MAX_NAMED]),
        "never_activated_total": len(never_activated),
        "stalled": _names(db, tenant_id, stalled[:MAX_NAMED]),
        "stalled_total": len(stalled),
        "active_in_window": len(active_ids),
        "completed_in_window": completed,
        "unclassified": split["unclassified"],
    }


def render(snap: dict, *, branding: str = "Dotmac Academy") -> tuple[str, str]:
    rows = [
        ("Staff on a course", snap["staff"]),
        ("Studied this period", snap["active_in_window"]),
        ("Never activated their account", snap.get("never_activated_total", 0)),
        ("Stalled (no activity this period)", snap.get("stalled_total", 0)),
        ("Courses completed", snap["completed_in_window"]),
    ]
    tr = "".join(
        f"<tr><td style='padding:6px 14px 6px 0;color:#5B6B62;'>{escape(label)}</td>"
        f"<td style='padding:6px 0;font-weight:600;'>{value}</td></tr>"
        for label, value in rows
    )

    def _block(title: str, names: list[str], total: int) -> tuple[str, str]:
        if not names:
            return "", ""
        more = f" (+{total - len(names)} more)" if total > len(names) else ""
        items = "".join(f"<li style='margin:2px 0;'>{escape(n)}</li>" for n in names)
        html = (
            f"<h2 style='font-size:15px;margin:18px 0 6px;'>{escape(title)}{escape(more)}</h2>"
            f"<ul style='margin:0;padding-left:18px;font-size:13px;'>{items}</ul>"
        )
        text = f"\n{title}{more}:\n" + "\n".join(f"- {n}" for n in names)
        return html, text

    na_html, na_text = _block("Cannot log in yet", snap["never_activated"], snap.get("never_activated_total", 0))
    st_html, st_text = _block("Stalled", snap["stalled"], snap.get("stalled_total", 0))

    caveat_html = caveat_text = ""
    if snap["unclassified"]:
        # Say what is missing. A roll-up that quietly omits part of the roster
        # invites HR to read it as complete.
        msg = (
            f"{snap['unclassified']} learner(s) are not yet classified as staff or external "
            "and are excluded from these figures."
        )
        caveat_html = f"<p style='margin-top:16px;font-size:12px;color:#5B6B62;'>{escape(msg)}</p>"
        caveat_text = f"\n\nNote: {msg}"

    html = (
        "<div style=\"font-family:system-ui,-apple-system,'Segoe UI',sans-serif;color:#0D1F16;"
        'max-width:560px;margin:0 auto;padding:24px;">'
        f"<p style='font-size:12px;letter-spacing:.14em;text-transform:uppercase;"
        f"color:#0B4F31;font-weight:600;margin:0 0 4px;'>{escape(branding)}</p>"
        "<h1 style='font-size:22px;margin:0 0 4px;'>Staff training report</h1>"
        f"<p style='margin:0 0 16px;color:#5B6B62;font-size:13px;'>Since {snap['since']:%d %b %Y}</p>"
        f"<table style='border-collapse:collapse;font-size:14px;'>{tr}</table>"
        f"{na_html}{st_html}{caveat_html}</div>"
    )
    text = (
        f"{branding} — staff training report (since {snap['since']:%d %b %Y})\n\n"
        + "\n".join(f"- {label}: {value}" for label, value in rows)
        + na_text
        + st_text
        + caveat_text
        + "\n"
    )
    return html, text


def send_hr_report(
    db: Session,
    *,
    tenant_id: UUID,
    recipients: list[str],
    days: int = 7,
    branding: str = "Dotmac Academy",
    now: datetime | None = None,
) -> int:
    """Queue the staff roll-up to HR. Returns the number of new messages queued."""
    now = now or datetime.now(UTC)
    since = now - timedelta(days=days)
    snap = snapshot(db, tenant_id=tenant_id, since=since, now=now)
    html, text = render(snap, branding=branding)
    period = now.strftime("%G-W%V")
    queued = 0
    for recipient in dict.fromkeys(r.strip() for r in recipients if r and r.strip()):
        if enqueue_email(
            db,
            tenant_id=tenant_id,
            idempotency_key=f"hr-report:{period}:{recipient}",
            kind="hr_training_report",
            recipient=recipient,
            subject=f"Staff training report — {now:%d %b %Y}",
            html_body=html,
            text_body=text,
        ):
            queued += 1
    return queued
