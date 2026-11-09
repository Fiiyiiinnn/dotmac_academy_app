"""Shared pagination values for server-rendered list pages."""

# Ported at new-composition genesis (web layer wave).

from __future__ import annotations

from math import ceil


def pagination_context(*, total: int, limit: int, offset: int) -> dict[str, object]:
    """Return display and navigation values, including compact page numbers."""
    total_pages = max(1, ceil(total / limit))
    current_page = min(total_pages, (offset // limit) + 1)
    wanted = {1, total_pages}
    wanted.update(range(max(1, current_page - 1), min(total_pages, current_page + 1) + 1))
    page_items: list[int | None] = []
    previous = 0
    for page in sorted(wanted):
        if previous and page - previous > 1:
            page_items.append(None)
        page_items.append(page)
        previous = page
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "current_page": current_page,
        "total_pages": total_pages,
        "page_items": page_items,
        "page_start": offset + 1 if total else 0,
        "page_end": min(offset + limit, total),
        "has_prev": offset > 0,
        "has_next": offset + limit < total,
    }
