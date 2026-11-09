"""Bulk backfill of ERP applicants into Academy admissions.

Thin batch wrapper over the intake owner
``app.services.admissions.submit_external_application`` — the single
writer of applicant intake. Idempotent per row on
``(tenant, external_ref)``; rows without an email are skipped.
Flushes; the caller owns the transaction.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.services.admissions import submit_external_application

SOURCE = "erp_backfill"
DEFAULT_PROGRAM = "Fiber Academy"


def _applied_on(raw: Any) -> date | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    text = str(raw).strip()
    try:
        return date.fromisoformat(text)
    except ValueError:
        return datetime.fromisoformat(text).date()


def backfill(db: Session, *, tenant_id: UUID, rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """Import ERP applicant rows; returns imported/skipped/total counts."""
    imported = 0
    skipped = 0
    total = 0
    for row in rows:
        total += 1
        email = str(row.get("email") or "").strip()
        if not email:
            skipped += 1
            continue
        applicant, created = submit_external_application(
            db,
            tenant_id=tenant_id,
            external_ref=str(row["external_ref"]),
            email=email,
            first_name=str(row.get("first_name") or ""),
            last_name=str(row.get("last_name") or ""),
            source=SOURCE,
            applied_on=_applied_on(row.get("applied_on")),
        )
        if created:
            if applicant.program is None:
                applicant.program = str(row.get("program") or DEFAULT_PROGRAM)
            if applicant.phone is None and row.get("phone"):
                applicant.phone = str(row["phone"])
        imported += 1
    db.flush()
    return {"imported": imported, "skipped": skipped, "total": total}
