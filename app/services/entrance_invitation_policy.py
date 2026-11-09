"""Platform-wide Settings policy for entrance-assessment invitation deadlines.

Applicant/token/outbox effects remain in the caller's existing tenant scope.
An explicit command duration is validated directly and does not re-read defaults.
"""

# Ported at new-composition genesis (admissions services slice).

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.admissions_settings import validate_entrance_invitation_deadline_days
from app.services.settings_store import effective


@dataclass(frozen=True, slots=True)
class EntranceInvitationPolicy:
    deadline_days: int

    def __post_init__(self):
        # Settings/store parsing is upstream; typed command choices are strict.
        if type(self.deadline_days) is not int:
            raise ValueError("entrance_invitation_deadline_days must be a positive integer")
        validate_entrance_invitation_deadline_days(self.deadline_days)

    def deadline(self, now: datetime) -> datetime:
        # Reconstruct the validated scalar to refuse tampered frozen snapshots.
        selected = EntranceInvitationPolicy(self.deadline_days)
        try:
            return now + timedelta(days=selected.deadline_days)
        except (ValueError, OverflowError):
            raise ValueError("entrance invitation deadline is not representable") from None


def entrance_invitation_policy(db: Session, *, deadline_days: int | None = None) -> EntranceInvitationPolicy:
    """Resolve an omitted default once from the actual DB-over-env store.

    The existing store is global to the Academy deployment, not per-tenant.
    Explicit CLI/service command durations preserve their independent choice.
    """
    if deadline_days is not None:
        return EntranceInvitationPolicy(deadline_days)
    configured = effective(db, require_store=True)
    try:
        days = configured.entrance_invitation_deadline_days
    except AttributeError:
        raise ValueError("missing effective entrance invitation policy") from None
    return EntranceInvitationPolicy(days)
