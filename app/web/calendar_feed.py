"""Authenticated iCalendar feed (P1 item 10).

Token auth only — calendar apps cannot send session cookies. An invalid,
expired, or missing token is a 404 (the feed should be indistinguishable
from absent without a valid token).
"""

# Ported at new-composition genesis (web layer wave).

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_tenant
from app.services import ical as ical_service
from app.services.settings_store import effective

router = APIRouter(dependencies=[Depends(require_tenant)])


@router.get("/calendar.ics")
def calendar_feed(
    request: Request,
    token: str = "",
    db: Session = Depends(get_db),
):
    tenant = require_tenant(request)
    now = datetime.now(UTC)
    person_id = ical_service.resolve_feed_token(db, tenant_id=tenant.id, raw=token, now=now)
    private = {"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer"}
    if person_id is None:
        return JSONResponse(status_code=404, content={"detail": "Not Found"}, headers=private)
    events = ical_service.events_for_person(db, tenant_id=tenant.id, person_id=person_id, now=now)
    policy = effective(db, require_store=True)
    body = ical_service.render_feed(events, calname=cast(str, policy["branding_name"]), now=now)
    return Response(
        content=body,
        media_type="text/calendar; charset=utf-8",
        headers={**private, "Content-Disposition": f'attachment; filename="{policy["ical_feed_filename"]}"'},
    )
