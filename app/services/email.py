"""Email subsystem — inert-by-default SMTP sender + notification helpers.

Design rules (see project memory / Inc1):

* Uses ONLY the Python stdlib (``smtplib`` + ``email.message.EmailMessage``);
  no new third-party dependency.
* INERT WHEN UNCONFIGURED: if ``settings.smtp_host`` is empty/None,
  :func:`send_email` logs and returns ``False``. It NEVER raises and NEVER
  blocks, so an unconfigured VM behaves as "email disabled". The operator
  enables email purely by setting SMTP_* env vars.
* NON-FATAL: every public entry point swallows exceptions and returns a bool.
  Sending an email must never break grading or a web request.

These helpers ``flush`` only when they read via the passed-in session; they
never ``commit`` (the caller owns the transaction boundary).
"""

# Ported at new-composition genesis (delivery-core slice).

from __future__ import annotations

import html as html_lib
import logging
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage

from dotmac_learning import has_other_passing_score
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmailResult:
    sent: bool
    error: str = ""


def send_email_detailed(
    to: str,
    subject: str,
    html_body: str,
    text_body: str | None = None,
    db: Session | None = None,
    attachments: list[tuple[str, bytes, str]] | None = None,
    message_id: str | None = None,
) -> EmailResult:
    """Send an email and return operator-facing delivery diagnostics.

    ``attachments`` is a list of ``(filename, data, mimetype)`` tuples, e.g.
    ``("certificate.pdf", pdf_bytes, "application/pdf")``.
    """
    from app.services.settings_store import effective

    cfg = effective(db)
    if not cfg.smtp_host:
        msg = "SMTP host is empty"
        logger.info("SMTP not configured (SMTP_HOST empty); skipping email to %s: %s", to, subject)
        return EmailResult(False, msg)
    if not to:
        msg = "recipient address is empty"
        logger.info("No recipient address; skipping email: %s", subject)
        return EmailResult(False, msg)
    try:
        email_msg = EmailMessage()
        email_msg["From"] = cfg.smtp_from
        email_msg["To"] = to
        email_msg["Subject"] = subject
        if message_id:
            email_msg["Message-ID"] = message_id
        email_msg.set_content(text_body or "This message requires an HTML-capable email client.")
        email_msg.add_alternative(html_body, subtype="html")
        for filename, data, mimetype in attachments or []:
            maintype, _, subtype = mimetype.partition("/")
            email_msg.add_attachment(
                data, maintype=maintype or "application", subtype=subtype or "octet-stream", filename=filename
            )

        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=15) as smtp:
            if cfg.smtp_starttls:
                smtp.starttls()
            if cfg.smtp_user:
                smtp.login(cfg.smtp_user, cfg.smtp_password)
            smtp.send_message(email_msg)
        logger.info("sent email to %s: %s", to, subject)
        return EmailResult(True)
    except Exception as exc:  # never let an email failure propagate
        error_class = type(exc).__name__
        logger.warning("failed to send email to %s (%s): %s", to, subject, error_class)
        return EmailResult(False, error_class)


def send_email(
    to: str,
    subject: str,
    html_body: str,
    text_body: str | None = None,
    db: Session | None = None,
    attachments: list[tuple[str, bytes, str]] | None = None,
    message_id: str | None = None,
) -> bool:
    """Send an HTML email (with optional plain-text alternative + attachments).

    SMTP config is read from the platform settings store (DB-over-env) when a
    ``db`` session is supplied; otherwise the env/config defaults in
    :data:`app.config.settings` are used. Returns ``True`` only if the message
    was handed to the SMTP server. Returns ``False`` (and logs) when SMTP is
    unconfigured or on ANY exception — it never raises, so callers can treat
    email as best-effort.
    """
    return send_email_detailed(
        to,
        subject,
        html_body,
        text_body=text_body,
        db=db,
        attachments=attachments,
        message_id=message_id,
    ).sent


def recipient_allows(person, kind: str) -> bool:
    """Whether ``person`` opts in to a notification of the given ``kind``.

    Reads the ``person.prefs`` JSONB dict. A missing key means opted IN — every
    notification is on by default, and a user must explicitly toggle it off on
    their Account → Notifications page. ``kind`` is e.g. ``"email_results"`` or
    ``"email_digest"``. Tolerant of a None person/prefs.
    """
    prefs = getattr(person, "prefs", None) or {}
    return bool(prefs.get(kind, True))


def notify_score_if_first_pass(
    db: Session, *, score, activity, person, first_pass: bool | None = None, raise_on_failure: bool = False
) -> bool:
    """Queue congratulations IFF this is the student's FIRST passing score.

    Fires only when ``score.passed`` is true AND there is no other passing
    score for the same (tenant, activity, person). Each optional intent
    owns a savepoint; catches run after rollback so a database refusal cannot
    poison the caller's grading transaction. No caller rollback or commit.
    """
    try:
        with db.begin_nested():
            return _queue_first_pass_intents(
                db,
                score=score,
                activity=activity,
                person=person,
                first_pass=first_pass,
                raise_on_failure=raise_on_failure,
            )
    except Exception as exc:  # non-fatal: grading must still succeed
        if raise_on_failure:
            raise
        logger.warning("notify_score_if_first_pass failed: %s", exc)
        return False


def _queue_first_pass_intents(
    db: Session, *, score, activity, person, first_pass: bool | None = None, raise_on_failure: bool = False
) -> bool:
    """Evaluate eligibility inside the public helper's savepoint."""
    if not getattr(score, "passed", False):
        return False
    if person is None or not getattr(person, "email", None):
        return False
    # Respect the recipient's own opt-out (Account → Notifications).
    if not recipient_allows(person, "email_results"):
        return False
    # Respect the platform "email on first pass" toggle (DB-over-env).
    from app.services.settings_store import effective

    if not effective(db).email_auto_on_pass:
        return False

    if first_pass is False:
        return False
    if first_pass is None and has_other_passing_score(
        db,
        tenant_id=score.tenant_id,
        person_id=person.id,
        activity_id=activity.id,
        excluded_score_id=score.id,
    ):
        return False

    subject = f"You passed {activity.title}"
    name_text = (getattr(person, "first_name", "") or "there").strip()
    name = html_lib.escape(name_text)
    activity_title = html_lib.escape(activity.title)
    pct = f"{score.fraction * 100:.0f}%"
    html = (
        f"<p>Hi {name},</p>"
        f"<p>Congratulations — you passed <strong>{activity_title}</strong> "
        f"with a score of {pct}.</p>"
        f"<p>Keep up the great work!</p>"
        f"<p>— Dotmac Academy</p>"
    )
    text = (
        f"Hi {name_text},\n\n"
        f"Congratulations — you passed {activity.title} with a score of {pct}.\n\n"
        f"Keep up the great work!\n\n— Dotmac Academy\n"
    )
    try:
        from app.services.notifications import notify as _notify

        with db.begin_nested():
            _notify(
                db,
                tenant_id=score.tenant_id,
                person_id=person.id,
                kind="result",
                title=f"You passed {activity.title}",
                body=f"Score: {pct}",
            )
    except Exception as _exc:
        if raise_on_failure:
            _exc.__dict__["_academy_delivery_category"] = "notify"
            raise
        logger.warning("in-app notify (result) failed: %s", _exc)
    from app.services.email_outbox import enqueue_email

    try:
        with db.begin_nested():
            return enqueue_email(
                db,
                tenant_id=score.tenant_id,
                idempotency_key=f"score-pass:{score.id}",
                kind="score_pass",
                recipient=person.email,
                subject=subject,
                html_body=html,
                text_body=text,
            )
    except Exception as exc:
        if raise_on_failure:
            exc.__dict__["_academy_delivery_category"] = "enqueue"
            raise
        logger.warning("score-pass email intent failed: %s", exc)
        return False


def render_transcript_html(transcript: dict) -> str:
    """Render a student transcript (from ``reports.student_transcript``) to HTML."""
    person = transcript["person"]
    rows = transcript["rows"]
    name = f"{person.first_name} {person.last_name}".strip()
    parts = [
        f"<h2>Transcript — {name}</h2>",
        f"<p>{person.email}</p>",
        '<table border="1" cellpadding="6" cellspacing="0">',
        "<tr><th>Activity</th><th>Type</th><th>Score</th><th>Result</th></tr>",
    ]
    if not rows:
        parts.append('<tr><td colspan="4">No graded activities yet.</td></tr>')
    for row in rows:
        act = row["activity"]
        result = "Pass" if row["passed"] else "Fail"
        parts.append(
            f"<tr><td>{act.title}</td><td>{act.type}</td><td>{row['fraction'] * 100:.0f}%</td><td>{result}</td></tr>"
        )
    parts.append("</table>")
    return "\n".join(parts)


def render_cohort_html(matrix: dict) -> str:
    """Render a cohort progress matrix (from ``reports.cohort_matrix``) to HTML."""
    cohort = matrix["cohort"]
    rows = matrix["rows"]
    parts = [
        f"<h2>Cohort progress — {cohort.name}</h2>",
        '<table border="1" cellpadding="6" cellspacing="0">',
        "<tr><th>Student</th><th>Email</th><th>Completion</th></tr>",
    ]
    if not rows:
        parts.append('<tr><td colspan="3">No enrolled students.</td></tr>')
    for row in rows:
        parts.append(f"<tr><td>{row['name']}</td><td>{row['email']}</td><td>{row['completion'] * 100:.0f}%</td></tr>")
    parts.append("</table>")
    return "\n".join(parts)
