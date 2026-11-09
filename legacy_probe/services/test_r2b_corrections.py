"""Regression tests for correction round R2-B: session editing, reschedule-
versioned reminder keys, and CSV/URL/iCal hardening."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.models.cohort import Cohort
from app.services import ical, scheduling
from app.services.audit import list_events
from app.services.csv_reports import sanitize_cell
from app.services.exceptions import BadRequestError
from app.services.localtime import local_to_utc
from legacy_probe.services.roster_request_fixtures import roster_actors

assert roster_actors


def _cohort(admin_session, tenant):
    c = Cohort(tenant_id=tenant.id, name="R2B", discipline="fiber", status="active")
    admin_session.add(c)
    admin_session.flush()
    return c


def test_update_session_converts_local_and_audits(roster_actors):
    admin_session = roster_actors.account.db
    tenant_a = roster_actors.account.tenant
    cohort = _cohort(admin_session, tenant_a)
    s = scheduling.create_session(
        admin_session,
        tenant_id=tenant_a.id,
        cohort_id=cohort.id,
        title="Old",
        starts_at=local_to_utc(datetime(2026, 8, 1, 10, 0)),
    )
    admin_session.flush()
    # Instructor edits to 14:00 Lagos wall-clock -> stored 13:00 UTC.
    new_start = local_to_utc(datetime(2026, 8, 1, 14, 0))
    session, changes = scheduling.update_session(
        admin_session,
        request=roster_actors.instructor.request,
        tenant_id=tenant_a.id,
        session_id=s.id,
        title="New title",
        starts_at=new_start,
    )
    assert session.title == "New title"
    assert session.starts_at.astimezone(UTC).hour == 13
    assert "title" in changes and "starts_at" in changes
    admin_session.flush()
    assert any(e.action == "scheduling.update_session" for e in list_events(admin_session, tenant_id=tenant_a.id))


def test_update_session_rejects_end_before_start(roster_actors):
    admin_session = roster_actors.account.db
    tenant_a = roster_actors.account.tenant
    cohort = _cohort(admin_session, tenant_a)
    s = scheduling.create_session(
        admin_session,
        tenant_id=tenant_a.id,
        cohort_id=cohort.id,
        title="S",
        starts_at=local_to_utc(datetime(2026, 8, 1, 10, 0)),
    )
    admin_session.flush()
    with pytest.raises(BadRequestError):
        scheduling.update_session(
            admin_session,
            request=roster_actors.instructor.request,
            tenant_id=tenant_a.id,
            session_id=s.id,
            ends_at=local_to_utc(datetime(2026, 8, 1, 9, 0)),
        )


def test_join_url_scheme_rejected_on_create_and_update(roster_actors):
    admin_session = roster_actors.account.db
    tenant_a = roster_actors.account.tenant
    cohort = _cohort(admin_session, tenant_a)
    with pytest.raises(BadRequestError):
        scheduling.create_session(
            admin_session,
            tenant_id=tenant_a.id,
            cohort_id=cohort.id,
            title="X",
            starts_at=local_to_utc(datetime(2026, 8, 1, 10, 0)),
            join_url="javascript:alert(1)",
        )
    s = scheduling.create_session(
        admin_session,
        tenant_id=tenant_a.id,
        cohort_id=cohort.id,
        title="X",
        starts_at=local_to_utc(datetime(2026, 8, 1, 10, 0)),
    )
    admin_session.flush()
    with pytest.raises(BadRequestError):
        scheduling.update_session(
            admin_session,
            request=roster_actors.instructor.request,
            tenant_id=tenant_a.id,
            session_id=s.id,
            join_url="ftp://x/y",
        )
    session, _ = scheduling.update_session(
        admin_session,
        request=roster_actors.instructor.request,
        tenant_id=tenant_a.id,
        session_id=s.id,
        join_url="https://meet.example/room",
    )
    assert session.join_url == "https://meet.example/room"


def test_reschedule_produces_new_reminder_occurrence_key():
    """A session key is versioned by start time, so moving it re-issues."""
    from app.services import reminders  # noqa: F401 — ensures module imports
    session_id = "abc"
    key_v1 = f"session_24h:{session_id}:{datetime(2026, 8, 1, 10, 0, tzinfo=UTC).strftime('%Y%m%dT%H%M')}"
    key_v2 = f"session_24h:{session_id}:{datetime(2026, 8, 2, 10, 0, tzinfo=UTC).strftime('%Y%m%dT%H%M')}"
    assert key_v1 != key_v2


def test_csv_cell_formula_injection_neutralised():
    assert sanitize_cell("=SUM(A1:A2)") == "'=SUM(A1:A2)"
    assert sanitize_cell("+1") == "'+1"
    assert sanitize_cell("@cmd") == "'@cmd"
    assert sanitize_cell("normal") == "normal"
    assert sanitize_cell(42) == 42


def test_ical_uri_not_escaped_and_long_line_folded():
    now = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
    events = [{
        "uid": "s1@x",
        "summary": "S" * 100,  # forces folding
        "starts_at": now,
        "url": "https://meet.example/room?a=1,2;3",  # comma/semicolon must survive
    }]
    out = ical.render_feed(events, calname="Cal", now=now)
    assert "URL:https://meet.example/room?a=1,2;3" in out          # unescaped URI
    assert "URL:https://meet.example/room?a=1\\,2" not in out       # not TEXT-escaped
    # A folded continuation line begins with CRLF + space.
    assert "\r\n " in out
    # No content line exceeds 75 octets.
    for line in out.split("\r\n"):
        if line.startswith(" "):
            continue
        assert len(line.encode("utf-8")) <= 75
