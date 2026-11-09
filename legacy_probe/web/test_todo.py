"""Unified To-Do timeline + calendar feed token flow (roadmap P1 items 7 & 10)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models.assessment import Activity
from app.models.cohort import Cohort, Enrollment
from app.models.course import Course
from app.models.offering import CourseOffering
from app.models.pacing import OfferingActivity
from app.services.security import hash_password
from app.services.todo import timeline_for_person
from legacy_probe.conftest import grant_role, make_person_credential, make_person_party, web_login

H = {"Host": "alpha.localhost"}

def _learner(admin_session, tenant, email="todo-stu@a.edu"):
    p = make_person_party(admin_session, tenant_id=tenant.id, email=email, first_name="To", last_name="Do")
    admin_session.add(p)
    admin_session.flush()
    admin_session.add(make_person_credential(admin_session, tenant_id=tenant.id, party_id=p.id, email=email, password_hash=hash_password("password1")))
    grant_role(admin_session, tenant_id=tenant.id, party_id=p.id)
    admin_session.commit()
    return p

def _login(app_client, email="todo-stu@a.edu"):
    web_login(app_client, H, email, "password1")

def _course_setup(admin_session, tenant, person, *, due_offsets_hours):
    """Cohort + course + active offering + one dated activity per offset."""
    cohort = Cohort(tenant_id=tenant.id, name="Todo Cohort", discipline="fiber", status="active")
    admin_session.add(cohort)
    course = Course(tenant_id=tenant.id, slug="todo-course", title="Todo Course",
                    discipline="fiber", source_ref="t@1", status="published", listed=False)
    admin_session.add(course)
    admin_session.flush()
    admin_session.add(Enrollment(tenant_id=tenant.id, cohort_id=cohort.id, person_id=person.id,
                                 role_in_cohort="student", status="active"))
    off = CourseOffering(tenant_id=tenant.id, cohort_id=cohort.id, course_id=course.id, status="active")
    admin_session.add(off)
    admin_session.flush()
    acts = []
    for n, hours in enumerate(due_offsets_hours):
        act = Activity(tenant_id=tenant.id, course_id=course.id, chapter_number=n + 1,
                       type="mcq_test", title=f"Deadline act {n}", pass_threshold=0.6, grading="auto", weight=1.0)
        admin_session.add(act)
        admin_session.flush()
        admin_session.add(OfferingActivity(tenant_id=tenant.id, offering_id=off.id, activity_id=act.id,
                                           due_at=datetime.now(UTC) + timedelta(hours=hours)))
        acts.append(act)
    admin_session.commit()
    return cohort, course, off, acts

def test_todo_requires_login(app_client, tenant_a):
    r = app_client.get("/todo", headers=H, follow_redirects=False)
    assert r.status_code in (302, 303)
    assert "/login" in r.headers.get("location", "")

def test_timeline_sections_deadlines_correctly(app_client, admin_session, tenant_a):
    p = _learner(admin_session, tenant_a)
    # -2h overdue; +26h lands beyond today (WAT day end) but inside 7 days for
    # any run time; +10 days lands in the 30-day bucket.
    _course_setup(admin_session, tenant_a, p, due_offsets_hours=[-2, 26, 240])
    t = timeline_for_person(admin_session, tenant_id=tenant_a.id, person_id=p.id)
    titles = lambda rows: [r["activity"].title for r in rows]  # noqa: E731
    assert titles(t["overdue"]) == ["Deadline act 0"]
    assert "Deadline act 1" in titles(t["due_today"]) + titles(t["next_7"])
    assert titles(t["next_30"]) == ["Deadline act 2"]

def test_todo_page_renders_sections(app_client, admin_session, tenant_a):
    p = _learner(admin_session, tenant_a)
    _course_setup(admin_session, tenant_a, p, due_offsets_hours=[-2])
    _login(app_client)
    r = app_client.get("/todo", headers=H)
    assert r.status_code == 200
    assert "Overdue" in r.text and "Deadline act 0" in r.text
    assert "Calendar export" in r.text

def test_feed_token_mint_and_rotate(app_client, admin_session, tenant_a):
    _learner(admin_session, tenant_a)
    _login(app_client)
    app_client.get("/todo", headers=H)
    csrf = app_client.cookies.get("csrf_token", "")
    r = app_client.post("/todo/calendar-feed", headers={**H, "x-csrf-token": csrf}, data={})
    assert r.status_code == 200
    token1 = r.text.split("calendar.ics?token=")[1].split("<")[0]
    assert len(token1) == 64

    feed = app_client.get(f"/calendar.ics?token={token1}", headers=H)
    assert feed.status_code == 200
    assert feed.headers["content-type"].startswith("text/calendar")
    assert "BEGIN:VCALENDAR" in feed.text

    # Rotation invalidates the first token.
    r2 = app_client.post("/todo/calendar-feed", headers={**H, "x-csrf-token": csrf}, data={})
    token2 = r2.text.split("calendar.ics?token=")[1].split("<")[0]
    assert token2 != token1
    assert app_client.get(f"/calendar.ics?token={token1}", headers=H).status_code == 404
    assert app_client.get(f"/calendar.ics?token={token2}", headers=H).status_code == 200

def test_feed_rejects_missing_or_bogus_token(app_client, tenant_a):
    assert app_client.get("/calendar.ics", headers=H).status_code == 404
    assert app_client.get("/calendar.ics?token=" + "0" * 64, headers=H).status_code == 404

def test_feed_contains_session_vevent(app_client, admin_session, tenant_a):
    from app.models.class_session import ClassSession

    p = _learner(admin_session, tenant_a)
    cohort, *_ = _course_setup(admin_session, tenant_a, p, due_offsets_hours=[50])
    s = ClassSession(tenant_id=tenant_a.id, cohort_id=cohort.id, session_type="live_class",
                     title="Feed Live Session", status="scheduled",
                     starts_at=datetime.now(UTC) + timedelta(hours=3),
                     ends_at=datetime.now(UTC) + timedelta(hours=4))
    admin_session.add(s)
    admin_session.commit()

    _login(app_client)
    app_client.get("/todo", headers=H)
    csrf = app_client.cookies.get("csrf_token", "")
    r = app_client.post("/todo/calendar-feed", headers={**H, "x-csrf-token": csrf}, data={})
    token = r.text.split("calendar.ics?token=")[1].split("<")[0]
    feed = app_client.get(f"/calendar.ics?token={token}", headers=H).text
    assert "SUMMARY:Feed Live Session" in feed
    assert f"UID:session-{s.id}@academy.dotmac.io" in feed
    assert "SUMMARY:Due: Deadline act 0" in feed
    assert "\r\n" in feed
