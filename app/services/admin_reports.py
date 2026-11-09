"""Admin activity report — the daily email that keeps humans in the loop.

With admissions auto-progression live, no admin touches the pipeline day to
day; this report is the visibility that replaces them. One email per admin
per tenant summarising the reporting window (applications, sittings, auto
decisions, enrolments, learning activity) plus the current pipeline state.

Window metrics use authoritative record timestamps. Automated admissions
decisions come from the audit transition ledger rather than mutable notes.

Learner-side reporting is a **read-only projection**, never a second opinion:
activity comes from the ``LearningEvent`` ledger, and who needs attention comes
from the open Success Queue exactly as ``services.success_queue`` decided it.
This module owns no rule and re-derives no threshold — if the queue is wrong,
it is wrong here too, which is the intended failure mode.
"""

# Ported at new-composition genesis (learning-facing wave 2).

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from html import escape
from typing import cast
from uuid import UUID

from dotmac_kernel.audit import AuditEvent
from dotmac_kernel.models import Party, PartyPerson, PartyRoleGrant, PartyType, Role
from dotmac_learning import (
    ReportPerson,
    activity_window_counts,
)
from dotmac_learning import (
    attention_list as learning_attention_list,
)
from dotmac_learning import (
    engagement_snapshot as learning_engagement_snapshot,
)
from dotmac_learning import (
    top_movers as learning_top_movers,
)
from sqlalchemy import func, select
from sqlalchemy.engine import Row
from sqlalchemy.orm import Session

from app.models.admissions import Applicant
from app.models.identity_extensions import AcademyAccountProfile
from app.services import settings_store
from app.services.email_outbox import enqueue_email

# Reason kinds rendered with a human label; anything the Success Queue adds
# later still shows up, falling back to its slug.
_REASON_LABELS = {
    "inactivity": "inactive",
    "overdue_work": "overdue work",
    "below_passing": "below passing",
    "failed_final": "failed final",
    "certificate_blocked": "certificate blocked",
}


def _count(db: Session, stmt) -> int:
    return int(db.scalar(stmt) or 0)


def _name(person: ReportPerson) -> str | None:
    return f"{person.first_name} {person.last_name}".strip() or person.email


def engagement_snapshot(db: Session, *, tenant_id: UUID, since: datetime) -> dict:
    """Serialize the learning owner's roster-wide engagement projection."""
    snapshot = learning_engagement_snapshot(db, tenant_id=tenant_id, since=since)
    return {
        "enrollees": snapshot.enrollees,
        "ever_active": snapshot.ever_active,
        "active_in_window": snapshot.active_in_window,
        "never_started": snapshot.never_started,
    }


def top_movers(db: Session, *, tenant_id: UUID, since: datetime, limit: int | None = None) -> list[dict]:
    """Render the owner's busiest learners in its existing rank order."""
    if limit is None:
        limit = cast(int, settings_store.effective(db, require_store=True)["learning_top_movers_limit"])
    movers = learning_top_movers(db, tenant_id=tenant_id, since=since, limit=limit)
    return [
        {
            "name": _name(mover.person),
            "email": mover.person.email,
            "cohort": mover.cohort_name,
            "events": mover.events,
            "submissions": mover.submissions,
            "avg_score": mover.avg_score,
        }
        for mover in movers
    ]


def attention_list(db: Session, *, tenant_id: UUID, limit: int | None = None) -> dict:
    """Render open queue facts without re-deriving rules or severity."""
    if limit is None:
        limit = cast(int, settings_store.effective(db, require_store=True)["learning_attention_limit"])
    snapshot = learning_attention_list(db, tenant_id=tenant_id, limit=limit)
    return {
        "open_total": snapshot.open_total,
        "by_reason": {item.reason: item.count for item in snapshot.by_reason},
        "never_started": snapshot.never_started,
        "top": [
            {
                "name": _name(item.person),
                "email": item.person.email,
                "cohort": item.cohort_name,
                "reason": _REASON_LABELS.get(item.reason, item.reason),
                "severity": item.severity,
                "days_inactive": item.days_inactive,
                "never_active": item.never_active,
            }
            for item in snapshot.top
        ],
    }


def activity_snapshot(db: Session, *, tenant_id: UUID, since: datetime) -> dict:
    """Gather the window + pipeline numbers the report renders."""
    A = Applicant
    base = select(func.count()).select_from(A).where(A.tenant_id == tenant_id)

    new_applications = _count(db, base.where(A.created_at >= since))
    sittings = _count(db, base.where(A.assessment_taken_at >= since))
    sittings_valid = _count(db, base.where(A.assessment_taken_at >= since).where(A.assessment_valid.is_(True)))
    avg_score = db.scalar(
        select(func.avg(A.assessment_score))
        .where(A.tenant_id == tenant_id)
        .where(A.assessment_taken_at >= since)
        .where(A.assessment_valid.is_(True))
    )
    decision_events = (
        select(func.count())
        .select_from(AuditEvent)
        .where(AuditEvent.tenant_id == tenant_id)
        .where(AuditEvent.action == "applicant.transition")
        .where(AuditEvent.created_at >= since)
        .where(AuditEvent.details.op("->>")("source") == "assessment_policy")
    )
    auto_accepted = _count(
        db,
        decision_events.where(AuditEvent.details.op("->>")("to_status") == "onboarding"),
    )
    auto_waitlisted = _count(
        db,
        decision_events.where(AuditEvent.details.op("->>")("to_status") == "waitlisted"),
    )
    invalid_awaiting = _count(
        db,
        base.where(A.status == "applied")
        .where(A.assessment_taken_at.is_not(None))
        .where(A.assessment_valid.is_not(True)),
    )

    pipeline_rows = db.execute(select(A.status, func.count()).where(A.tenant_id == tenant_id).group_by(A.status)).all()

    learning_counts = activity_window_counts(db, tenant_id=tenant_id, since=since)

    return {
        "since": since,
        "new_applications": new_applications,
        "sittings": sittings,
        "sittings_valid": sittings_valid,
        "avg_valid_score": float(avg_score) if avg_score is not None else None,
        "auto_accepted": auto_accepted,
        "auto_waitlisted": auto_waitlisted,
        "invalid_awaiting_review": invalid_awaiting,
        "pipeline": {status: n for status, n in pipeline_rows},
        "new_enrollments": learning_counts.new_enrollments,
        "submissions": learning_counts.submissions,
        "completions": learning_counts.completions,
        "engagement": engagement_snapshot(db, tenant_id=tenant_id, since=since),
        "top_movers": top_movers(db, tenant_id=tenant_id, since=since),
        "attention": attention_list(db, tenant_id=tenant_id),
    }


def admin_recipients(db: Session, *, tenant_id: UUID) -> list[Row]:
    """Active admin-role people with an email address."""
    return list(
        db.execute(
            select(Party.id, Party.email)
            .join(PartyPerson, PartyPerson.party_id == Party.id)
            .join(
                AcademyAccountProfile,
                (AcademyAccountProfile.party_id == Party.id) & (AcademyAccountProfile.tenant_id == Party.tenant_id),
            )
            .join(
                PartyRoleGrant,
                (PartyRoleGrant.party_id == Party.id) & (PartyRoleGrant.tenant_id == Party.tenant_id),
            )
            .join(Role, (Role.id == PartyRoleGrant.role_id) & (Role.tenant_id == PartyRoleGrant.tenant_id))
            .where(Party.tenant_id == tenant_id, Party.party_type == PartyType.person)
            .where(AcademyAccountProfile.status == "active")
            .where(Role.slug == "admin")
            .where(Party.email.is_not(None))
        ).unique(lambda recipient: recipient.id)
    )


def _mover_line(m: dict) -> str:
    """One top-mover as plain text: 'Name — 12 events, 4 submissions, avg 84% (Cohort)'."""
    bits = [f"{m['events']} events", f"{m['submissions']} submissions"]
    if m["avg_score"] is not None:
        bits.append(f"avg {m['avg_score']:.0%}")
    tail = f" ({m['cohort']})" if m["cohort"] else ""
    return f"{m['name']} — {', '.join(bits)}{tail}"


def _attention_line(a: dict) -> str:
    """One attention row: 'Name — inactive 21d, never started (Cohort)'."""
    reason = a["reason"]
    if a["days_inactive"] is not None:
        reason = f"{reason} {a['days_inactive']}d"
    if a["never_active"]:
        reason = f"{reason}, never started"
    tail = f" ({a['cohort']})" if a["cohort"] else ""
    return f"{a['name']} — {reason}{tail}"


def _render(snapshot: dict) -> tuple[str, str]:
    """(html, text) bodies for the report email."""
    s = snapshot
    avg = f"{s['avg_valid_score']:.0%}" if s["avg_valid_score"] is not None else "—"
    pipeline = ", ".join(f"{k}: {v}" for k, v in sorted(s["pipeline"].items())) or "empty"
    eng = s["engagement"]
    att = s["attention"]
    reasons = ", ".join(f"{k}: {v}" for k, v in sorted(att["by_reason"].items())) or "none"
    rows = [
        ("New applications", s["new_applications"]),
        ("Entrance sittings graded", f"{s['sittings']} ({s['sittings_valid']} valid, avg {avg})"),
        ("Auto-accepted", s["auto_accepted"]),
        ("Auto-waitlisted", s["auto_waitlisted"]),
        ("Invalid sittings awaiting review", s["invalid_awaiting_review"]),
        ("New enrolments", s["new_enrollments"]),
        ("Activity submissions", s["submissions"]),
        ("Course completions", s["completions"]),
        (
            "Learners active in window",
            f"{eng['active_in_window']} of {eng['enrollees']} enrolled",
        ),
        ("Never started (no activity ever)", eng["never_started"]),
        ("Open Success Queue items", f"{att['open_total']} ({reasons})"),
    ]
    movers = "".join(f"<li style='margin:2px 0;'>{escape(_mover_line(m))}</li>" for m in s["top_movers"])
    attention = "".join(f"<li style='margin:2px 0;'>{escape(_attention_line(a))}</li>" for a in att["top"])
    lists = ""
    if movers:
        lists += (
            "<h2 style='font-size:15px;margin:20px 0 6px;'>Top movers this window</h2>"
            f"<ul style='margin:0;padding-left:18px;font-size:13px;color:#0D1F16;'>{movers}</ul>"
        )
    if attention:
        remainder = att["open_total"] - len(att["top"])
        more = (
            f"<p style='margin:6px 0 0;font-size:12px;color:#5B6B62;'>+{remainder} more in the Success Queue</p>"
            if remainder > 0
            else ""
        )
        lists += (
            "<h2 style='font-size:15px;margin:20px 0 6px;'>Needs attention</h2>"
            f"<ul style='margin:0;padding-left:18px;font-size:13px;color:#0D1F16;'>{attention}</ul>{more}"
        )
    tr = "".join(
        f"<tr><td style='padding:6px 14px 6px 0;color:#5B6B62;'>{label}</td>"
        f"<td style='padding:6px 0;font-weight:600;'>{value}</td></tr>"
        for label, value in rows
    )
    html = (
        "<div style=\"font-family:system-ui,-apple-system,'Segoe UI',sans-serif;color:#0D1F16;"
        'max-width:560px;margin:0 auto;padding:24px;">'
        "<p style='font-size:12px;letter-spacing:.14em;text-transform:uppercase;"
        "color:#0B4F31;font-weight:600;margin:0 0 4px;'>Dotmac Academy</p>"
        f"<h1 style='font-size:22px;margin:0 0 4px;'>Activity report</h1>"
        f"<p style='margin:0 0 16px;color:#5B6B62;font-size:13px;'>Since {s['since']:%d %b %Y %H:%M} UTC</p>"
        f"<table style='border-collapse:collapse;font-size:14px;'>{tr}</table>"
        f"{lists}"
        f"<p style='margin-top:16px;font-size:13px;color:#5B6B62;'>Pipeline now — {pipeline}</p>"
        "</div>"
    )
    text = f"Dotmac Academy activity report (since {s['since']:%d %b %Y %H:%M} UTC)\n\n" + "\n".join(
        f"- {label}: {value}" for label, value in rows
    )
    if s["top_movers"]:
        text += "\n\nTop movers this window:\n" + "\n".join(f"- {_mover_line(m)}" for m in s["top_movers"])
    if att["top"]:
        text += "\n\nNeeds attention:\n" + "\n".join(f"- {_attention_line(a)}" for a in att["top"])
        remainder = att["open_total"] - len(att["top"])
        if remainder > 0:
            text += f"\n- (+{remainder} more in the Success Queue)"
    text += f"\n\nPipeline now: {pipeline}\n"
    return html, text


def send_activity_report(db: Session, *, tenant_id: UUID, hours: int | None = None) -> int:
    """Queue the activity report for every Academy admin."""
    if hours is None:
        hours = cast(int, settings_store.effective(db, require_store=True)["learning_activity_report_hours"])
    since = datetime.now(UTC) - timedelta(hours=hours)
    snapshot = activity_snapshot(db, tenant_id=tenant_id, since=since)
    html, text = _render(snapshot)
    subject = f"Academy activity report — {datetime.now(UTC):%d %b %Y}"
    sent = 0
    for admin in admin_recipients(db, tenant_id=tenant_id):
        if enqueue_email(
            db,
            tenant_id=tenant_id,
            idempotency_key=f"admin-report:{admin.id}:{datetime.now(UTC):%Y-%m-%d}:{hours}",
            kind="admin_report",
            recipient=admin.email,
            subject=subject,
            html_body=html,
            text_body=text,
        ):
            sent += 1
    return sent
