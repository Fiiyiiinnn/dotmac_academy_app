"""Learner timetable + session detail + agenda links (roadmap P0 items 2-4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from dotmac_kernel.models import PartyRoleGrant

from app.models.class_session import ClassSession
from app.models.cohort import Cohort, Enrollment
from app.services.agenda import upcoming_for_person
from app.services.scheduling import join_is_open
from app.services.security import hash_password
from legacy_probe.conftest import grant_role, make_person_credential, make_person_party, web_login

H = {"Host": "alpha.localhost"}

def _learner(admin_session, tenant, email="tt-stu@a.edu"):
    p = make_person_party(admin_session, tenant_id=tenant.id, email=email, first_name="Tt", last_name="Stu")
    admin_session.add(p)
    admin_session.flush()
    admin_session.add(
        make_person_credential(admin_session, tenant_id=tenant.id, party_id=p.id, email=email, password_hash=hash_password("password1"))
    )
    grant_role(admin_session, tenant_id=tenant.id, party_id=p.id)
    admin_session.commit()
    return p

def _login(app_client, email="tt-stu@a.edu"):
    web_login(app_client, H, email, "password1")

def _cohort_with_session(admin_session, tenant, person=None, *, name="TZ Cohort", starts_in_hours=2.0, join_url=None):
    c = Cohort(tenant_id=tenant.id, name=name, discipline="fiber", status="active", delivery_mode="blended")
    admin_session.add(c)
    admin_session.flush()
    if person is not None:
        admin_session.add(
            Enrollment(
                tenant_id=tenant.id, cohort_id=c.id, person_id=person.id, role_in_cohort="student", status="active"
            )
        )
    s = ClassSession(
        tenant_id=tenant.id,
        cohort_id=c.id,
        session_type="live_class",
        title=f"Live for {name}",
        status="scheduled",
        starts_at=datetime.now(UTC) + timedelta(hours=starts_in_hours),
        ends_at=datetime.now(UTC) + timedelta(hours=starts_in_hours + 1),
        join_url=join_url,
    )
    admin_session.add(s)
    admin_session.commit()
    admin_session.refresh(s)
    return c, s

def test_timetable_requires_login(app_client, tenant_a):
    r = app_client.get("/timetable", headers=H, follow_redirects=False)
    assert r.status_code in (302, 303)
    assert "/login" in r.headers.get("location", "")

def test_timetable_lists_only_my_cohort_sessions(app_client, admin_session, tenant_a):
    p = _learner(admin_session, tenant_a)
    _cohort_with_session(admin_session, tenant_a, p, name="Mine")
    _cohort_with_session(admin_session, tenant_a, None, name="Other")
    _login(app_client)
    r = app_client.get("/timetable", headers=H)
    assert r.status_code == 200
    assert "Live for Mine" in r.text
    assert "Live for Other" not in r.text
    # Timezone label is derived from the configured academy zone (Africa/Lagos → WAT).
    assert "WAT" in r.text

def test_session_detail_own_and_foreign(app_client, admin_session, tenant_a):
    p = _learner(admin_session, tenant_a, email="tt-det@a.edu")
    _c, mine = _cohort_with_session(admin_session, tenant_a, p, name="DetMine", join_url="https://meet.example/x")
    _c2, other = _cohort_with_session(admin_session, tenant_a, None, name="DetOther")
    _login(app_client, email="tt-det@a.edu")
    r = app_client.get(f"/timetable/sessions/{mine.id}", headers=H)
    assert r.status_code == 200
    assert "Live for DetMine" in r.text and "DetMine" in r.text
    r = app_client.get(f"/timetable/sessions/{other.id}", headers=H)
    assert r.status_code == 404

def test_agenda_session_links_to_detail_not_bare_timetable(admin_session, tenant_a):
    p = _learner(admin_session, tenant_a, email="tt-ag@a.edu")
    _c, s = _cohort_with_session(admin_session, tenant_a, p, name="AgCo")
    items = upcoming_for_person(admin_session, tenant_id=tenant_a.id, person_id=p.id)
    sess = [i for i in items if i["kind"] == "session"]
    assert sess and sess[0]["link"] == f"/timetable/sessions/{s.id}"
    assert all(i.get("link") != "/timetable" for i in items)

def test_join_window_rules(admin_session, tenant_a):
    p = _learner(admin_session, tenant_a, email="tt-join@a.edu")
    now = datetime.now(UTC)
    _c, soon = _cohort_with_session(
        admin_session, tenant_a, p, name="Soon", starts_in_hours=0.5, join_url="https://meet.example/soon"
    )
    _c2, far = _cohort_with_session(
        admin_session, tenant_a, p, name="Far", starts_in_hours=48, join_url="https://meet.example/far"
    )
    _c3, nolink = _cohort_with_session(admin_session, tenant_a, p, name="NoLink", starts_in_hours=0.5)
    assert join_is_open(soon, now=now, lead_minutes=60) is True
    assert join_is_open(far, now=now, lead_minutes=60) is False
    assert join_is_open(nolink, now=now, lead_minutes=60) is False
    # In-progress session with a link is still joinable.
    soon.starts_at = now - timedelta(minutes=10)
    soon.ends_at = now + timedelta(minutes=50)
    assert join_is_open(soon, now=now, lead_minutes=60) is True

def test_instructor_input_stored_as_utc_from_lagos(app_client, admin_session, tenant_a):
    from sqlalchemy import select

    from app.services.bootstrap import ensure_roles

    roles = ensure_roles(admin_session, tenant_a.id)
    p = _learner(admin_session, tenant_a, email="tt-inst@a.edu")
    admin_session.add(PartyRoleGrant(tenant_id=tenant_a.id, party_id=p.id, role_id=roles["instructor"].id))
    c = Cohort(tenant_id=tenant_a.id, name="TZ Input", discipline="fiber", status="active", delivery_mode="self_paced")
    admin_session.add(c)
    admin_session.commit()
    _login(app_client, email="tt-inst@a.edu")
    csrf = app_client.cookies.get("csrf_token", "")
    r = app_client.post(
        f"/instructor/cohorts/{c.id}/sessions",
        headers={**H, "x-csrf-token": csrf, "HX-Request": "true"},
        data={"title": "Lagos 10am", "starts_at": "2026-08-01T10:00"},
    )
    assert r.status_code == 200
    s = admin_session.scalars(select(ClassSession).where(ClassSession.cohort_id == c.id)).first()
    assert s is not None
    # 10:00 Lagos (UTC+1) == 09:00 UTC
    assert s.starts_at.astimezone(UTC).hour == 9
