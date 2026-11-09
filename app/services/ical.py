"""iCalendar feed — hand-rolled RFC 5545 output plus feed-token management.

The feed is a read-only projection of the To-Do timeline (sessions +
deadlines). Auth is a single-purpose, long-lived AccountToken of kind
``ical_feed`` — multi-use by design (calendar apps poll), rotated by minting
a replacement, and never the session cookie (roadmap P1 item 10).
"""

# Ported at new-composition genesis (learning-facing wave 2).

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.account_token import AccountToken
from app.services.canonical_identity_store import lock_parties, lock_person
from app.services.security import hash_token
from app.services.settings_store import effective
from app.services.todo import timeline_for_person

ICAL_TOKEN_KIND = "ical_feed"  # noqa: S105 — token *kind* label, not a secret


def mint_feed_token(db: Session, *, tenant_id: UUID, person_id: UUID, now: datetime | None = None) -> str:
    """Create (or rotate) the person's feed token; returns the raw value once.

    Rotation = expire every previous ical_feed token so exactly one works.
    """
    now = now or datetime.now(UTC)
    # Match canonical lifecycle lock order: principal before person subtype.
    # This also serializes rotations when no previous feed tokens exist.
    lock_parties(db, tenant_id=tenant_id, party_ids=(person_id,))
    if lock_person(db, tenant_id=tenant_id, party_id=person_id) is None:
        raise ValueError("calendar feed requires a tenant-local person")
    policy = effective(db, require_store=True)
    db.execute(
        update(AccountToken)
        .where(
            AccountToken.tenant_id == tenant_id,
            AccountToken.person_id == person_id,
            AccountToken.kind == ICAL_TOKEN_KIND,
        )
        .values(expires_at=now)
    )
    raw = uuid4().hex + uuid4().hex
    db.add(
        AccountToken(
            tenant_id=tenant_id,
            person_id=person_id,
            kind=ICAL_TOKEN_KIND,
            token_hash=hash_token(raw),
            expires_at=now + timedelta(days=cast(int, policy["ical_feed_token_ttl_days"])),
        )
    )
    db.flush()
    return raw


def has_feed_token(db: Session, *, tenant_id: UUID, person_id: UUID, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    return (
        db.scalars(
            select(AccountToken.id)
            .where(AccountToken.tenant_id == tenant_id)
            .where(AccountToken.person_id == person_id)
            .where(AccountToken.kind == ICAL_TOKEN_KIND)
            .where(AccountToken.expires_at > now)
            .where(AccountToken.used_at.is_(None))
        ).first()
        is not None
    )


def resolve_feed_token(db: Session, *, tenant_id: UUID, raw: str, now: datetime | None = None) -> UUID | None:
    """Return the person_id for a valid feed token, else None. Multi-use."""
    if not raw:
        return None
    now = now or datetime.now(UTC)
    person_id = db.scalars(
        select(AccountToken.person_id)
        .where(AccountToken.tenant_id == tenant_id)
        .where(AccountToken.kind == ICAL_TOKEN_KIND)
        .where(AccountToken.token_hash == hash_token(raw))
        .where(AccountToken.expires_at > now)
        .where(AccountToken.used_at.is_(None))
    ).first()
    return person_id


# --- RFC 5545 rendering -----------------------------------------------------


def _esc(value: str) -> str:
    return (
        (value or "")
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "\\n")
    )


def _uri(value: str) -> str:
    """URI-typed property value (URL:) — RFC 5545 forbids TEXT backslash-escaping
    here, so a URL containing ',' or ';' must be emitted verbatim. Only strip
    CR/LF, which would otherwise inject extra content lines."""
    return (value or "").replace("\r", "").replace("\n", "")


def _fold(line: str) -> str:
    """RFC 5545 content-line folding: no line may exceed 75 octets; a fold is
    CRLF + a single leading space. Fold on octet boundaries (UTF-8 safe)."""
    chunks: list[str] = []
    chunk = ""
    size = 0
    limit = 75
    for char in line:
        width = len(char.encode("utf-8"))
        if size + width > limit:
            chunks.append(chunk)
            chunk, size, limit = "", 0, 74
        chunk += char
        size += width
    chunks.append(chunk)
    return "\r\n ".join(chunks)


def _stamp(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def render_feed(events: list[dict], *, calname: str, now: datetime) -> str:
    """Render VCALENDAR text (CRLF line endings) from event dicts.

    Event dict: uid, summary, starts_at, ends_at (optional), description
    (optional), location (optional), url (optional).
    """
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Dotmac Academy//Learning Calendar//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_esc(calname)}",
    ]
    for e in events:
        start = e["starts_at"]
        end = e.get("ends_at")
        lines += [
            "BEGIN:VEVENT",
            f"UID:{_esc(e['uid'])}",
            f"DTSTAMP:{_stamp(now)}",
            f"DTSTART:{_stamp(start)}",
            f"SUMMARY:{_esc(e['summary'])}",
        ]
        if end is not None:
            lines.append(f"DTEND:{_stamp(end)}")
        if e.get("description"):
            lines.append(f"DESCRIPTION:{_esc(e['description'])}")
        if e.get("location"):  # LOCATION is TEXT-typed → escaped
            lines.append(f"LOCATION:{_esc(e['location'])}")
        if e.get("url"):  # URL is URI-typed → NOT backslash-escaped (RFC 5545)
            lines.append(f"URL:{_uri(e['url'])}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


def events_for_person(db: Session, *, tenant_id: UUID, person_id: UUID, now: datetime) -> list[dict]:
    """Flatten the To-Do projection into feed events (sessions + deadlines)."""
    t = timeline_for_person(db, tenant_id=tenant_id, person_id=person_id, now=now)
    policy = effective(db, require_store=True)
    domain = policy["ical_feed_uid_domain"]
    events: list[dict] = []
    for s, cohort_name in t["sessions"]:
        events.append(
            {
                "uid": f"session-{s.id}@{domain}",
                "summary": s.title,
                "starts_at": s.starts_at,
                "ends_at": s.ends_at,
                "description": (
                    f"{cohort_name} — live session"
                    if s.session_type == policy["learning_default_session_type"]
                    else cohort_name
                ),
                "location": s.location or "",
                "url": s.join_url or "",
            }
        )
    for bucket in ("overdue", "due_today", "next_7", "next_30"):
        for row in t[bucket]:
            act = row["activity"]
            events.append(
                {
                    "uid": f"due-{act.id}@{domain}",
                    "summary": f"Due: {act.title}",
                    "starts_at": row["due_at"],
                    "description": row["course_title"],
                }
            )
    return events
