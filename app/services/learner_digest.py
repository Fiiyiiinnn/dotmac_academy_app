"""Weekly progress email for learners — what you did, where you are, what's next.

The Academy has never told a learner how they are doing. The existing weekly
digest (``cli.email-digest``) is a *cohort matrix for instructors*, and it has
never sent a single message: only four instructor enrolments exist against 202
students, and the setting gating it has been off since 2026-07-20. Learners had
to log in and look, which is precisely what a disengaged learner does not do.

This is a read-only projection. Course state comes from
``learner_dashboard.course_cards`` — the owner of that derivation — so the email
and the dashboard can never disagree about whether a course is in progress.
Delivery is the outbox's; this module only decides who gets what and when.
"""

# Ported at new-composition genesis (learning-facing wave 2).

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from html import escape
from uuid import UUID

from dotmac_kernel.models import Party, PartyPerson, PartyType
from dotmac_learning import learner_window_activity
from dotmac_learning.participation_models import Enrollment
from sqlalchemy import select
from sqlalchemy.engine import Row
from sqlalchemy.orm import Session

from app.models.identity_extensions import AcademyAccountProfile
from app.services import learner_dashboard
from app.services.email import recipient_allows
from app.services.email_outbox import enqueue_email

# Opt-out key, shared with the Account → Notifications page. Distinct from the
# instructor "email_digest" so a learner silencing their own progress mail does
# not also silence an instructor report they may separately receive.
OPTOUT_KIND = "email_learner_digest"

# At most this many courses are itemised; the rest are summarised as a count.
# A weekly nudge is a prompt, not a transcript.
MAX_COURSE_LINES = 5


def _display_name(person: Row) -> str:
    return (person.first_name or "").strip() or "there"


def week_activity(db: Session, *, tenant_id: UUID, person_id: UUID, since: datetime) -> dict:
    """What this learner actually did in the window."""
    activity = learner_window_activity(db, tenant_id=tenant_id, person_id=person_id, since=since)
    return {"chapters": activity.chapters, "submissions": activity.submissions, "passed": activity.passed}


def _headline(activity: dict, name: str) -> str:
    """One sentence about the week, honest either way.

    A learner who did nothing is told so plainly rather than congratulated —
    an email that praises inactivity teaches people to ignore it.
    """
    if activity["passed"]:
        return f"You passed {activity['passed']} piece(s) of work this week."
    if activity["submissions"]:
        return f"You attempted {activity['submissions']} piece(s) of work this week."
    if activity["chapters"]:
        return f"You read {activity['chapters']} chapter(s) this week."
    return "You haven't studied this week — here's where you left off."


def build_digest(db: Session, *, tenant_id: UUID, person: Row, since: datetime, now: datetime) -> dict | None:
    """The digest payload for one learner, or None when there is nothing to say.

    Returns None only when the learner has no live course at all. A quiet week
    on a live course is exactly when the email is worth sending.
    """
    cards = learner_dashboard.course_cards(db, tenant_id=tenant_id, person_id=person.id, now=now)["cards"]
    live = [c for c in cards if c["state"] in {"in_progress", "upcoming"}]
    if not live:
        return None

    activity = week_activity(db, tenant_id=tenant_id, person_id=person.id, since=since)
    lines = []
    for card in live[:MAX_COURSE_LINES]:
        nxt = card["next_activity"]
        lines.append(
            {
                "title": card["course"].title,
                "pct": card["pct"],
                "passed": card["passed"],
                "total": card["total"],
                "next": nxt.title if nxt is not None else None,
                "href": (card["action"] or {}).get("href"),
                "deadline": card["next_deadline"],
            }
        )
    return {
        "name": _display_name(person),
        "headline": _headline(activity, _display_name(person)),
        "activity": activity,
        "courses": lines,
        "more_courses": max(len(live) - MAX_COURSE_LINES, 0),
    }


def render(digest: dict, *, base_url: str, branding: str) -> tuple[str, str]:
    """(html, text) bodies. Learner-supplied text is escaped; course titles are
    authored content but escaped anyway — an email body is not a trust boundary."""
    base = base_url.rstrip("/")
    rows_html = []
    rows_text = []
    for c in digest["courses"]:
        where = f"{c['pct']}% complete ({c['passed']} of {c['total']})"
        nxt = f" — next: {c['next']}" if c["next"] else ""
        due = f" (due {c['deadline']:%d %b})" if c["deadline"] else ""
        link = f"{base}{c['href']}" if c["href"] else base
        rows_html.append(
            f"<li style='margin:6px 0;'><a href='{escape(link)}' style='color:#0B4F31;font-weight:600;'>"
            f"{escape(c['title'])}</a> — {escape(where)}{escape(nxt)}{escape(due)}</li>"
        )
        rows_text.append(f"- {c['title']} — {where}{nxt}{due}\n  {link}")

    more = ""
    if digest["more_courses"]:
        more = f"<p style='font-size:13px;color:#5B6B62;'>+{digest['more_courses']} more course(s) in your account.</p>"

    html = (
        "<div style=\"font-family:system-ui,-apple-system,'Segoe UI',sans-serif;color:#0D1F16;"
        'max-width:560px;margin:0 auto;padding:24px;">'
        f"<p style='font-size:12px;letter-spacing:.14em;text-transform:uppercase;"
        f"color:#0B4F31;font-weight:600;margin:0 0 4px;'>{escape(branding)}</p>"
        f"<h1 style='font-size:22px;margin:0 0 10px;'>Hi {escape(digest['name'])}, here's your week</h1>"
        f"<p style='margin:0 0 16px;'>{escape(digest['headline'])}</p>"
        f"<ul style='margin:0;padding-left:18px;font-size:14px;'>{''.join(rows_html)}</ul>"
        f"{more}"
        f"<p style='margin-top:20px;'><a href='{escape(base)}/' "
        "style='background:#0B4F31;color:#fff;padding:10px 16px;border-radius:8px;"
        "text-decoration:none;font-weight:600;'>Continue learning</a></p>"
        "<p style='margin-top:20px;font-size:12px;color:#5B6B62;'>"
        "You can turn these weekly emails off under Account → Notifications.</p>"
        "</div>"
    )
    text = (
        f"Hi {digest['name']}, here's your week\n\n"
        f"{digest['headline']}\n\n" + "\n".join(rows_text) + f"\n\nContinue learning: {base}/\n\n"
        "You can turn these weekly emails off under Account → Notifications.\n"
    )
    return html, text


def send_weekly_digests(
    db: Session,
    *,
    tenant_id: UUID,
    base_url: str,
    branding: str = "Dotmac Academy",
    now: datetime | None = None,
) -> dict:
    """Queue one digest per active student learner. Returns per-outcome counts.

    Idempotent per ISO week: re-running in the same week enqueues nothing new,
    so a retried or double-scheduled job cannot double-send.
    """
    now = now or datetime.now(UTC)
    since = now - timedelta(days=7)
    period = now.strftime("%G-W%V")
    counts = {"queued": 0, "skipped_optout": 0, "skipped_no_courses": 0}

    learners = db.execute(
        select(Party.id, Party.email, PartyPerson.first_name, AcademyAccountProfile.prefs)
        .join(PartyPerson, PartyPerson.party_id == Party.id)
        .join(
            AcademyAccountProfile,
            (AcademyAccountProfile.party_id == Party.id) & (AcademyAccountProfile.tenant_id == Party.tenant_id),
        )
        .join(Enrollment, (Enrollment.person_id == Party.id) & (Enrollment.tenant_id == Party.tenant_id))
        .where(Party.tenant_id == tenant_id)
        .where(
            Party.party_type == PartyType.person, Party.is_active.is_(True), AcademyAccountProfile.status == "active"
        )
        .where(Enrollment.status == "active")
        .where(Enrollment.role_in_cohort == "student")
        .distinct()
    )

    for person in learners:
        if not recipient_allows(person, OPTOUT_KIND):
            counts["skipped_optout"] += 1
            continue
        digest = build_digest(db, tenant_id=tenant_id, person=person, since=since, now=now)
        if digest is None:
            counts["skipped_no_courses"] += 1
            continue
        html, text = render(digest, base_url=base_url, branding=branding)
        if enqueue_email(
            db,
            tenant_id=tenant_id,
            idempotency_key=f"learner-digest:{period}:{person.id}",
            kind="learner_digest",
            recipient=person.email,
            subject=f"Your week at {branding}",
            html_body=html,
            text_body=text,
        ):
            counts["queued"] += 1
    return counts
