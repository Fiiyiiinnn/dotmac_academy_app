"""Scheduling — class-session timetable, delivery mode, and agenda reminders."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.services import scheduling
from app.services.agenda import upcoming_for_person
from app.services.exceptions import BadRequestError
from legacy_probe.conftest import make_person_party


def _cohort(admin_session, tenant, name="Live cohort", mode="self_paced"):
    from app.models.cohort import Cohort

    admin_session.rollback()
    c = Cohort(tenant_id=tenant.id, name=name, discipline="fiber", status="active", delivery_mode=mode)
    admin_session.add(c)
    admin_session.commit()
    admin_session.refresh(c)
    return c

def _soon(**kw):
    return datetime.now(UTC) + timedelta(**kw)

def test_create_session_and_timetable(admin_session, tenant_a):
    cohort = _cohort(admin_session, tenant_a)
    s1 = scheduling.create_session(
        admin_session,
        tenant_id=tenant_a.id,
        cohort_id=cohort.id,
        title="Fiber splicing — live",
        starts_at=_soon(days=2),
        ends_at=_soon(days=2, hours=2),
        session_type="live_class",
        location="Lab A",
    )
    scheduling.create_session(
        admin_session,
        tenant_id=tenant_a.id,
        cohort_id=cohort.id,
        title="OTDR lab",
        starts_at=_soon(days=1),
        session_type="lab",
    )
    admin_session.commit()

    tt = scheduling.list_for_cohort(admin_session, tenant_id=tenant_a.id, cohort_id=cohort.id)
    assert [s.title for s in tt] == ["OTDR lab", "Fiber splicing — live"]  # chronological
    assert s1.session_type == "live_class"
    # scheduling on a self_paced cohort promotes it to blended
    admin_session.refresh(cohort)
    assert cohort.delivery_mode == "blended"

def test_validation(admin_session, tenant_a):
    cohort = _cohort(admin_session, tenant_a)
    with pytest.raises(BadRequestError):
        scheduling.create_session(
            admin_session,
            tenant_id=tenant_a.id,
            cohort_id=cohort.id,
            title="x",
            starts_at=_soon(days=1),
            session_type="party",
        )
    with pytest.raises(BadRequestError):
        scheduling.create_session(
            admin_session,
            tenant_id=tenant_a.id,
            cohort_id=cohort.id,
            title="x",
            starts_at=_soon(days=1),
            ends_at=_soon(hours=1),
        )

def test_delivery_mode_and_cancel(admin_session, tenant_a):
    cohort = _cohort(admin_session, tenant_a)
    scheduling.set_delivery_mode(admin_session, tenant_id=tenant_a.id, cohort_id=cohort.id, mode="live")
    admin_session.refresh(cohort)
    assert cohort.delivery_mode == "live"
    with pytest.raises(BadRequestError):
        scheduling.set_delivery_mode(admin_session, tenant_id=tenant_a.id, cohort_id=cohort.id, mode="hologram")

    s = scheduling.create_session(
        admin_session, tenant_id=tenant_a.id, cohort_id=cohort.id, title="Lecture", starts_at=_soon(days=3)
    )
    scheduling.cancel_session(admin_session, tenant_id=tenant_a.id, session_id=s.id)
    s = scheduling.get_session(admin_session, tenant_id=tenant_a.id, session_id=s.id)
    assert s.status == "cancelled"

def test_session_appears_in_agenda_for_enrolled_person(admin_session, tenant_a):
    from app.models.cohort import Enrollment

    cohort = _cohort(admin_session, tenant_a)
    admin_session.rollback()
    p = make_person_party(admin_session, tenant_id=tenant_a.id, email="learner@a.ex", first_name="L", last_name="R")
    admin_session.add(p)
    admin_session.flush()
    admin_session.add(
        Enrollment(
            tenant_id=tenant_a.id, cohort_id=cohort.id, person_id=p.id, role_in_cohort="student", status="active"
        )
    )
    scheduling.create_session(
        admin_session,
        tenant_id=tenant_a.id,
        cohort_id=cohort.id,
        title="Live class",
        starts_at=_soon(days=1),
        join_url="https://meet",
    )
    admin_session.commit()

    agenda = upcoming_for_person(admin_session, tenant_id=tenant_a.id, person_id=p.id)
    sessions = [i for i in agenda if i["kind"] == "session"]
    # ADR: agenda links to the session detail page; the raw join_url rides along
    # for the calendar's imminent-Join button.
    assert any(
        i["title"] == "Live class" and i["link"].startswith("/timetable/sessions/") and i["join_url"] == "https://meet"
        for i in sessions
    )

def test_session_isolated_between_tenants(admin_session, tenant_a, tenant_b):
    from sqlalchemy import select as sa_select

    from app.models.class_session import ClassSession

    cohort = _cohort(admin_session, tenant_a)
    s = scheduling.create_session(
        admin_session, tenant_id=tenant_a.id, cohort_id=cohort.id, title="Secret", starts_at=_soon(days=1)
    )
    admin_session.commit()
    # admin_session bypasses RLS; assert the row is tagged to tenant_a only.
    row = admin_session.get(ClassSession, s.id)
    assert row.tenant_id == tenant_a.id
    # and nothing leaked to tenant_b's cohorts
    other = admin_session.scalars(sa_select(ClassSession).where(ClassSession.tenant_id == tenant_b.id)).all()
    assert s.id not in [x.id for x in other]
