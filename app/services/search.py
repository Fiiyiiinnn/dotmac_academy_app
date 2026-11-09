"""Search presentation facade; Learning owns tenant/access queries and caps."""

# Ported at new-composition genesis (learning-facing wave).

from __future__ import annotations

from typing import cast
from uuid import UUID

from dotmac_learning import SearchAuthority, search_learning
from sqlalchemy.orm import Session

from app.services.settings_store import effective


def search(
    db: Session,
    *,
    tenant_id: UUID,
    person_id: UUID,
    q: str,
    is_staff: bool,
) -> dict:
    results = search_learning(
        db,
        tenant_id=tenant_id,
        person_id=person_id,
        q=q,
        authority=SearchAuthority(tenant_id, person_id, is_staff),
        limit=cast(int, effective(db, require_store=True)["learning_search_limit"]),
    )
    return {
        "courses": [{"slug": course.slug, "title": course.title} for course in results.courses],
        "chapters": [
            {
                "title": hit.chapter.title,
                "course_slug": hit.course_slug,
                "chapter_number": hit.chapter.number,
                "snippet": _extract_snippet(hit.chapter.body_html, results.term),
            }
            for hit in results.chapters
        ],
    }


def _extract_snippet(html: str, term: str, radius: int = 100) -> str:
    """Return a short plain-text snippet around the first occurrence of term."""
    import re

    plain = re.sub(r"<[^>]+>", " ", html)
    plain = re.sub(r"\s+", " ", plain).strip()
    lower = plain.lower()
    idx = lower.find(term.lower())
    if idx == -1:
        return plain[: radius * 2]
    start = max(0, idx - radius)
    end = min(len(plain), idx + len(term) + radius)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(plain) else ""
    return prefix + plain[start:end] + suffix
