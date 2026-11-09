"""Applicant-facing emails — acknowledgement and the entrance-exam invitation.

The exam link used to be rendered ONCE on the /apply response page. If the
candidate closed the tab, the token was gone forever and there was no way back
in — which is how 180 applicants produced zero sittings. The invitation email is
the durable copy of that link.

Sends are best-effort: ``send_email`` never raises and returns False when SMTP
is unconfigured, so a mail failure can never block an application.
"""

# Ported at new-composition genesis (delivery-core slice).

from __future__ import annotations

import html
from datetime import UTC, datetime
from typing import TypedDict

from dotmac_learning.curriculum_models import QuestionBank
from dotmac_learning.models import Activity, Course
from dotmac_learning.participation_models import Cohort, CourseOffering, Track
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.admissions import Applicant
from app.services.email_outbox import enqueue_email
from app.services.exceptions import BadRequestError
from app.services.localtime import to_local


class AssessmentEmailItem(TypedDict):
    name: str
    course: str
    duration: str
    availability: str
    expiry: str


class AssessmentEmailContext(TypedDict):
    cohort: str
    track: str
    courses: list[str]
    assessments: list[AssessmentEmailItem]
    timezone: str


_WRAP = """\
<div style="font-family:system-ui,-apple-system,'Segoe UI',sans-serif;color:#0D1F16;
            max-width:560px;margin:0 auto;padding:24px;">
  <p style="font-size:12px;letter-spacing:.14em;text-transform:uppercase;
            color:#0B4F31;font-weight:600;margin:0 0 4px;">Dotmac Academy</p>
  <h1 style="font-size:22px;margin:0 0 16px;">{title}</h1>
  {body}
  <p style="margin-top:28px;font-size:12px;color:#5B6B62;">
    Dotmac Academy — please do not reply to this address.
  </p>
</div>"""

_BTN = """\
<p style="margin:24px 0;">
  <a href="{url}" style="background:#F4621A;color:#fff;text-decoration:none;
     padding:12px 22px;border-radius:6px;font-weight:600;display:inline-block;">
    Start the assessment
  </a>
</p>
<p style="font-size:13px;color:#5B6B62;">
  If the button doesn't work, paste this into your browser:<br>
  <span style="word-break:break-all;">{url}</span>
</p>"""


def _fmt_dt(value: datetime | None) -> str:
    if value is None:
        return "Not configured"
    local = to_local(value)
    assert local is not None
    zone = local.tzname() or settings.academy_timezone
    return f"{local.strftime('%A %d %B %Y at %H:%M')} {zone} ({settings.academy_timezone})"


def _assessment_context(db: Session, *, applicant: Applicant) -> AssessmentEmailContext:
    from app.services import entrance_exam

    cohort = db.get(Cohort, applicant.cohort_id) if applicant.cohort_id else None
    track = db.get(Track, applicant.track_id) if applicant.track_id else None
    courses: list[Course] = []
    assessments: list[AssessmentEmailItem] = []

    if cohort is not None:
        courses = list(
            db.scalars(
                select(Course)
                .join(
                    CourseOffering,
                    (CourseOffering.course_id == Course.id) & (CourseOffering.tenant_id == Course.tenant_id),
                )
                .where(Course.tenant_id == applicant.tenant_id)
                .where(CourseOffering.cohort_id == cohort.id)
                .where(CourseOffering.status == "active")
                .order_by(Course.title)
            ).all()
        )
        course_ids = [course.id for course in courses]
        if course_ids:
            for activity, course, offering in db.execute(
                select(Activity, Course, CourseOffering)
                .join(Course, (Course.id == Activity.course_id) & (Course.tenant_id == Activity.tenant_id))
                .join(
                    CourseOffering,
                    (CourseOffering.course_id == Course.id) & (CourseOffering.tenant_id == Course.tenant_id),
                )
                .where(Activity.tenant_id == applicant.tenant_id)
                .where(Activity.course_id.in_(course_ids))
                .where(CourseOffering.cohort_id == cohort.id)
                .where(CourseOffering.status == "active")
                .order_by(Course.title, Activity.chapter_number, Activity.title)
            ).all():
                assessments.append(
                    {
                        "name": activity.title,
                        "course": course.title,
                        "duration": "Not configured",
                        "availability": f"{_fmt_dt(offering.starts_at)} to {_fmt_dt(offering.ends_at)}",
                        "expiry": _fmt_dt(offering.ends_at),
                    }
                )

    try:
        bank_id = entrance_exam.resolve_bank_id(db, applicant=applicant)
        bank = db.get(QuestionBank, bank_id)
        entrance_course = db.get(Course, bank.course_id) if bank is not None else None
        minutes = entrance_exam.time_limit_minutes(db, applicant=applicant)
        assessments.insert(
            0,
            {
                "name": entrance_course.title if entrance_course is not None else "Entrance assessment",
                "course": entrance_course.title if entrance_course is not None else "Admissions",
                "duration": f"{minutes} minutes" if minutes else "Untimed",
                "availability": f"Now to {_fmt_dt(applicant.assessment_deadline)}",
                "expiry": _fmt_dt(applicant.assessment_deadline),
            },
        )
    except BadRequestError:
        pass

    return {
        "cohort": cohort.name if cohort is not None else "Not assigned",
        "track": track.name if track is not None else (applicant.program or "Not assigned"),
        "courses": [course.title for course in courses] or ([applicant.program] if applicant.program else []),
        "assessments": assessments,
        "timezone": settings.academy_timezone,
    }


def send_application_received(db: Session, *, applicant: Applicant) -> bool:
    """Acknowledge the application. Sent immediately on submit."""
    name = html.escape((applicant.first_name or "there").strip())
    body = (
        f"<p>Hi {name},</p>"
        "<p>We've received your application to the Dotmac Academy. Thank you.</p>"
        "<p>The next step is a short online entrance assessment. We've sent it to you "
        "in a separate email — check your inbox (and your spam folder).</p>"
    )
    queued = enqueue_email(
        db,
        tenant_id=applicant.tenant_id,
        idempotency_key=f"application-received:{applicant.id}",
        kind="application_received",
        recipient=applicant.email,
        subject="We've received your Dotmac Academy application",
        html_body=_WRAP.format(title="Application received", body=body),
        text_body=(
            f"Hi {name},\n\nWe've received your application to the Dotmac Academy.\n\n"
            "The next step is a short online entrance assessment — we've sent it in a "
            "separate email. Check your inbox and spam folder.\n"
        ),
    )
    return queued


def send_exam_invite(db: Session, *, applicant: Applicant, url: str, minutes: int | None) -> bool:
    """The entrance-exam invitation: instructions, the link, and the deadline.

    This is the durable copy of the token — the whole point of the email.
    """
    name = html.escape((applicant.first_name or "there").strip())
    deadline = applicant.assessment_deadline
    by = _fmt_dt(deadline) if deadline else None
    ctx = _assessment_context(db, applicant=applicant)
    courses = ctx["courses"]
    courses_html = "".join(f"<li>{html.escape(str(course))}</li>" for course in courses) or "<li>Not assigned</li>"
    courses_text = "; ".join(str(course) for course in courses) or "Not assigned"
    assessment_html = "".join(
        "<li>"
        f"<strong>{html.escape(item['name'])}</strong><br>"
        f"Duration: {html.escape(item['duration'])}<br>"
        f"Availability: {html.escape(item['availability'])}<br>"
        f"Expiration date: {html.escape(item['expiry'])}"
        "</li>"
        for item in ctx["assessments"]
    )
    assessment_text = "\n".join(
        f"- {item['name']} | Duration: {item['duration']} | "
        f"Availability: {item['availability']} | Expires: {item['expiry']}"
        for item in ctx["assessments"]
    )

    timing = (
        f"<li>It is <strong>timed: {minutes} minutes</strong> once you begin, and submits "
        "automatically when the time is up.</li>"
        if minutes
        else "<li>Take your time — there is no time limit.</li>"
    )
    rules = (
        "<ul style='line-height:1.7;'>"
        "<li>30 multiple-choice questions. Choose the single best answer.</li>"
        f"{timing}"
        "<li>You get <strong>one attempt</strong>, so start when you can finish uninterrupted.</li>"
        "<li>Your answers save as you go — if your connection drops, reopen the link and "
        "carry on where you left off.</li>"
        "<li>It tests general aptitude — numeracy, reading, reasoning, safety sense and basic "
        "technical understanding. <strong>You are not expected to know fibre already.</strong></li>"
        "</ul>"
    )
    body = (
        f"<p>Hi {name},</p>"
        "<p>Here is your Dotmac Academy entrance assessment.</p>"
        f"<p>Learner: <strong>{name}</strong></p>"
        f"<p>Cohort: <strong>{html.escape(str(ctx['cohort']))}</strong><br>"
        f"Track: <strong>{html.escape(str(ctx['track']))}</strong><br>"
        f"Timezone: <strong>{html.escape(str(ctx['timezone']))}</strong></p>"
        f"<p>Course(s):</p><ul>{courses_html}</ul>"
        + (
            f"<p>Assigned assessment(s):</p><ul style='line-height:1.7;'>{assessment_html}</ul>"
            if assessment_html
            else ""
        )
        + rules
        + (f"<p><strong>Your assessment link is valid until {by}.</strong></p>" if by else "")
        + _BTN.format(url=html.escape(url, quote=True))
        + "<p style='font-size:13px;color:#5B6B62;'>Trouble with the link, or cut off part-way? "
        "Reply to the team and we can reopen your sitting.</p>"
    )
    text = (
        f"Hi {name},\n\nYour Dotmac Academy entrance assessment:\n{url}\n\n"
        f"Learner: {name}\nCohort: {ctx['cohort']}\nTrack: {ctx['track']}\nTimezone: {ctx['timezone']}\n"
        f"Course(s): {courses_text}\n"
        + (f"Assigned assessment(s):\n{assessment_text}\n" if assessment_text else "")
        + "- 30 multiple-choice questions, one best answer each\n"
        + (f"- Timed: {minutes} minutes once you begin; it submits automatically at zero\n" if minutes else "")
        + "- One attempt — start when you can finish uninterrupted\n"
        "- Answers save as you go; if your connection drops, reopen the link and continue\n"
        "- It tests general aptitude, not fibre knowledge\n"
        + (f"\nYour assessment link is valid until {by}.\n" if by else "")
    )
    queued = enqueue_email(
        db,
        tenant_id=applicant.tenant_id,
        idempotency_key=f"entrance-invite:{applicant.id}:{applicant.assessment_token_hash}",
        kind="entrance_invite",
        recipient=applicant.email,
        subject="Your Dotmac Academy entrance assessment",
        html_body=_WRAP.format(title="Your entrance assessment", body=body),
        text_body=text,
        payload={"applicant_id": str(applicant.id)},
    )
    if queued:
        applicant.invite_sent_at = datetime.now(UTC)
        db.flush()
    return queued


def send_waitlist_notice(db: Session, *, applicant: Applicant) -> bool:
    """Result email for a valid sitting below the auto-accept bar."""
    name = html.escape((applicant.first_name or "there").strip())
    body = (
        f"<p>Hi {name},</p>"
        "<p>Thank you for completing your Dotmac Academy entrance assessment.</p>"
        "<p>Based on your result you've been placed on our <strong>waitlist</strong>. That's not a "
        "no — our admissions team reviews waitlisted applications individually, and we'll contact "
        "you if a place opens in your chosen track.</p>"
        "<p style='font-size:13px;color:#5B6B62;'>No action is needed from you right now.</p>"
    )
    text = (
        f"Hi {name},\n\nThank you for completing your Dotmac Academy entrance assessment.\n\n"
        "Based on your result you've been placed on our waitlist. That's not a no — our team "
        "reviews waitlisted applications individually, and we'll contact you if a place opens.\n"
    )
    return enqueue_email(
        db,
        tenant_id=applicant.tenant_id,
        idempotency_key=f"assessment-waitlist:{applicant.id}:{applicant.assessment_reset_count}",
        kind="assessment_waitlist",
        recipient=applicant.email,
        subject="Your Dotmac Academy application — waitlisted",
        html_body=_WRAP.format(title="You're on the waitlist", body=body),
        text_body=text,
    )


def send_results_received(db: Session, *, applicant: Applicant) -> bool:
    """Result email when no automatic decision was made (held for human review)."""
    name = html.escape((applicant.first_name or "there").strip())
    body = (
        f"<p>Hi {name},</p>"
        "<p>Thank you — your Dotmac Academy entrance assessment has been received.</p>"
        "<p>Our admissions team will review your application and be in touch by email "
        "with the outcome.</p>"
    )
    text = (
        f"Hi {name},\n\nThank you — your entrance assessment has been received. "
        "Our admissions team will review your application and be in touch by email.\n"
    )
    return enqueue_email(
        db,
        tenant_id=applicant.tenant_id,
        idempotency_key=f"assessment-review:{applicant.id}:{applicant.assessment_reset_count}",
        kind="assessment_review",
        recipient=applicant.email,
        subject="We've received your Dotmac Academy assessment",
        html_body=_WRAP.format(title="Assessment received", body=body),
        text_body=text,
    )


def send_onboarding_invite(db: Session, *, applicant: Applicant, url: str) -> bool:
    """The offer email: you're in — complete your onboarding online.

    This is the durable copy of the onboarding-portal token.
    """
    name = html.escape((applicant.first_name or "there").strip())
    track = html.escape((applicant.program or "").strip())
    track_line = f"<p>Track: <strong>{track}</strong></p>" if track else ""
    body = (
        f"<p>Hi {name},</p>"
        "<p>Good news — your Dotmac Academy application has been accepted.</p>"
        + track_line
        + "<p>One short step before your training starts: confirm your details and review "
        "the programme orientation. It takes a few minutes, all online.</p>"
        + _BTN.format(url=html.escape(url, quote=True)).replace("Start the assessment", "Complete your onboarding")
    )
    text = (
        f"Hi {name},\n\nGood news — your Dotmac Academy application has been accepted.\n\n"
        "Complete your onboarding (confirm your details and review the programme "
        f"orientation) here:\n{url}\n"
    )
    return enqueue_email(
        db,
        tenant_id=applicant.tenant_id,
        idempotency_key=f"onboarding-invite:{applicant.id}:{applicant.onboarding_token_hash}",
        kind="onboarding_invite",
        recipient=applicant.email,
        subject="You're in — complete your Dotmac Academy onboarding",
        html_body=_WRAP.format(title="Application accepted", body=body),
        text_body=text,
    )


def send_enrollment_welcome(db: Session, *, applicant: Applicant, setup_url: str | None) -> bool:
    """Enrolment confirmation. With ``setup_url``, invites the new student to set
    their password; without it (account already exists) points them at login."""
    name = html.escape((applicant.first_name or "there").strip())
    if setup_url:
        action = "<p>Set your password to open your student account and start your courses:</p>" + _BTN.format(
            url=html.escape(setup_url, quote=True)
        ).replace("Start the assessment", "Set your password")
        text_action = f"Set your password to start learning:\n{setup_url}\n"
    else:
        action = "<p>Your existing account now has student access — log in to start your courses.</p>"
        text_action = "Your existing account now has student access — log in to start your courses.\n"
    body = f"<p>Hi {name},</p><p>You are enrolled. Welcome to the Dotmac Academy.</p>" + action
    return enqueue_email(
        db,
        tenant_id=applicant.tenant_id,
        idempotency_key=f"enrollment-welcome:{applicant.id}:{applicant.person_id}",
        kind="enrollment_welcome",
        recipient=applicant.email,
        subject="You're enrolled — welcome to the Dotmac Academy",
        html_body=_WRAP.format(title="You're enrolled", body=body),
        text_body=f"Hi {name},\n\nYou are enrolled. Welcome to the Dotmac Academy.\n\n" + text_action,
    )
