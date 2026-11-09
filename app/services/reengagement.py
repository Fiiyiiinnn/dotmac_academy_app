"""Re-engagement of stranded learners — enrolled, but locked out of their own account.

A learner bulk-enrolled from an ERP roster has a Person row and a cohort seat
but never chose a password. To them the Academy is invisible: nudge emails point
at a login they cannot pass. Their *only* useful message is a fresh activation
link, so this module names that population and re-issues one.

Ownership: the stranded rule lives here; token minting follows the composed
stranded invitation policy (``invitation_issuance_policy(purpose="stranded")``)
and delivery stays with the outbox. This module decides *who*,
never *how* a token is made or a mail is sent. It is deliberately NOT an
at-risk detector — learners who can log in but don't are the Success Queue's,
and duplicating that rule here would create a second, drifting owner.
"""

# Ported at new-composition genesis (learning-facing wave).

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from uuid import UUID

from dotmac_kernel.models import AuthSession, Party, PartyPerson, PartyType, UserCredential
from dotmac_learning.participation_models import Enrollment
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.account_token import AccountToken
from app.models.identity_extensions import AcademyAccountProfile
from app.services.email_outbox import enqueue_email
from app.services.invitation_policy import invitation_issuance_policy
from app.services.security import hash_token


@dataclass(frozen=True, slots=True)
class StrandedLearner:
    """Projection of one stranded canonical account (id = Party id)."""

    id: UUID
    email: str
    first_name: str


# A re-issued invite must outlive a weekend and a slow inbox; the 7-day default
# matches the entrance-exam invitation wave so learners see one consistent window.
DEFAULT_TTL_HOURS = 24 * 7


def stranded_learners(db: Session, *, tenant_id: UUID, cohort_id: UUID | None = None) -> list[StrandedLearner]:
    """Active student enrollees with no credential and no session, ever.

    Both conditions matter: no credential means they cannot log in, and no
    session proves they never did. A learner who set a password and then went
    quiet is *not* stranded — they are the Success Queue's to chase.
    """
    stmt = (
        select(Party.id, Party.email, PartyPerson.first_name)
        .select_from(Party)
        .join(PartyPerson, PartyPerson.party_id == Party.id)
        .join(
            AcademyAccountProfile,
            (AcademyAccountProfile.party_id == Party.id) & (AcademyAccountProfile.tenant_id == Party.tenant_id),
        )
        .join(
            Enrollment,
            (Enrollment.person_id == Party.id) & (Enrollment.tenant_id == Party.tenant_id),
        )
        .where(Party.tenant_id == tenant_id)
        .where(Party.party_type == PartyType.person, Party.is_active.is_(True))
        .where(AcademyAccountProfile.status == "active")
        .where(Enrollment.status == "active")
        .where(Enrollment.role_in_cohort == "student")
        .where(
            ~select(UserCredential.id)
            .where(UserCredential.tenant_id == tenant_id)
            .where(UserCredential.party_id == Party.id)
            .exists()
        )
        .where(
            ~select(AuthSession.id)
            .where(AuthSession.tenant_id == tenant_id)
            .where(AuthSession.party_id == Party.id)
            .exists()
        )
        .order_by(Party.email)
        .distinct()
    )
    if cohort_id is not None:
        stmt = stmt.where(Enrollment.cohort_id == cohort_id)
    return [StrandedLearner(id=row.id, email=row.email, first_name=row.first_name) for row in db.execute(stmt)]


def _mint_stranded_invite(db: Session, *, tenant_id: UUID, person_id: UUID, now: datetime, ttl_hours: int) -> str:
    """Mint one invite token under the composed stranded policy.

    The human invitation wrappers require a live web actor session, which a
    CLI sweep does not have; the stranded purpose is prepared precisely as a
    separate policy (``INVITATION_STRANDED_REPLACE_OUTSTANDING``). Fixed
    protocol matches the composed issuer: 32 random bytes as hex, HMAC store.
    """
    policy = invitation_issuance_policy(db, purpose="stranded")
    if policy.replace_outstanding:
        outstanding = db.scalars(
            select(AccountToken)
            .where(
                AccountToken.tenant_id == tenant_id,
                AccountToken.person_id == person_id,
                AccountToken.kind == "invite",
                AccountToken.used_at.is_(None),
            )
            .with_for_update()
        ).all()
        for row in outstanding:
            row.used_at = now
    raw = secrets.token_hex(32)
    db.add(
        AccountToken(
            tenant_id=tenant_id,
            person_id=person_id,
            kind="invite",
            token_hash=hash_token(raw),
            expires_at=policy.deadline(now, ttl_hours=ttl_hours),
        )
    )
    db.flush()
    return raw


def _bodies(person: StrandedLearner, link: str) -> tuple[str, str]:
    """Activation copy. States why they are hearing from us, then the one action."""
    greeting = escape(person.first_name) or "there"
    html = (
        f"<p>Hi {greeting},</p>"
        "<p>You have a place on a Dotmac Academy course, but your account was never "
        "activated — so you have not been able to sign in or start.</p>"
        f'<p><a href="{link}">Activate your account and start learning</a></p>'
        f"<p>If the link does not work, paste this into your browser:<br>{link}</p>"
        "<p>The link is valid for 7 days. Reply to this email if you no longer want your place.</p>"
    )
    text = (
        f"Hi {person.first_name or 'there'},\n\n"
        "You have a place on a Dotmac Academy course, but your account was never "
        "activated — so you have not been able to sign in or start.\n\n"
        f"Activate your account and start learning: {link}\n\n"
        "The link is valid for 7 days. Reply to this email if you no longer want your place.\n"
    )
    return html, text


def reinvite_stranded(
    db: Session,
    *,
    tenant_id: UUID,
    base_url: str,
    cohort_id: UUID | None = None,
    ttl_hours: int = DEFAULT_TTL_HOURS,
    now: datetime | None = None,
) -> dict:
    """Mint a fresh activation token per stranded learner and queue the email.

    Each call mints a NEW token, which invalidates any older link that learner
    still holds — intended, since the old ones are the reason they are stranded.
    Returns {"targets", "queued", "failed"}; the caller commits.
    """
    now = now or datetime.now(UTC)
    targets = stranded_learners(db, tenant_id=tenant_id, cohort_id=cohort_id)
    queued = failed = 0
    for person in targets:
        token = _mint_stranded_invite(db, tenant_id=tenant_id, person_id=person.id, now=now, ttl_hours=ttl_hours)
        link = f"{base_url.rstrip('/')}/accept-invite?token={token}"
        html, text = _bodies(person, link)
        if enqueue_email(
            db,
            tenant_id=tenant_id,
            # Keyed on the token hash, so a retry of the same run is idempotent
            # while a deliberate re-run mints a new token and genuinely re-sends.
            idempotency_key=f"account-invite:{person.id}:{hash_token(token)}",
            kind="account_invite",
            recipient=person.email,
            subject="Activate your Dotmac Academy account",
            html_body=html,
            text_body=text,
        ):
            queued += 1
        else:
            failed += 1
    return {"targets": len(targets), "queued": queued, "failed": failed}
