"""Learner Success Hub dashboard slice (roadmap P1a).

Pins the learner_dashboard service contract and its rendering: card states
and filters, one action per state (Start vs Resume vs Review), the server-
side resume target, and the rich progress page.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models.assessment import Activity, Score, Submission
from app.models.cohort import Cohort, Enrollment
from app.models.completion import CourseCompletion
from app.models.course import Chapter, Course
from app.models.offering import CourseOffering
from app.models.pacing import OfferingActivity
from app.models.reading import ChapterRead
from app.services.security import hash_password
from legacy_probe.conftest import grant_role, make_person_credential, make_person_party, web_login

H = {"Host": "alpha.localhost"}

def _login(app_client, admin_session, tenant, email="dash@a.edu"):
    p = make_person_party(admin_session, tenant_id=tenant.id, email=email, first_name="Dash", last_name="Board")
    admin_session.add(p)
    admin_session.flush()
    admin_session.add(make_person_credential(admin_session, tenant_id=tenant.id, party_id=p.id, email=email, password_hash=hash_password("password1")))
    grant_role(admin_session, tenant_id=tenant.id, party_id=p.id)
    admin_session.commit()
    web_login(app_client, H, email, "password1")
    return p

def _course(admin_session, tid, slug, title, *, chapters=2, threshold=0.7):
    course = Course(tenant_id=tid, slug=slug, title=title,
                    discipline="networking", source_ref="x", version=1, status="published", listed=False)
    admin_session.add(course)
    admin_session.flush()
    acts = []
    for n in range(1, chapters + 1):
        admin_session.add(Chapter(tenant_id=tid, course_id=course.id, number=n,
                                  title=f"{title} Ch{n}", body_html="<p>x</p>"))
        a = Activity(tenant_id=tid, course_id=course.id, chapter_number=n,
                     type="mcq_test", title=f"{title} Test {n}",
                     pass_threshold=threshold, max_attempts=3, grading="auto", weight=1.0)
        admin_session.add(a)
        acts.append(a)
    admin_session.flush()
    return course, acts

def _offer(admin_session, tid, person_id, course_id, *, name, starts_at=None, ends_at=None):
    coh = Cohort(tenant_id=tid, name=name, discipline="networking", status="active")
    admin_session.add(coh)
    admin_session.flush()
    admin_session.add(Enrollment(tenant_id=tid, cohort_id=coh.id, person_id=person_id,
                                 role_in_cohort="student", status="active"))
    off = CourseOffering(tenant_id=tid, cohort_id=coh.id, course_id=course_id,
                         status="active", starts_at=starts_at, ends_at=ends_at)
    admin_session.add(off)
    admin_session.flush()
    return off

def _pass(admin_session, tid, person_id, activity_id, *, fraction=1.0, passed=True):
    sub = Submission(tenant_id=tid, activity_id=activity_id, person_id=person_id,
                     answers={}, attempt_no=1)
    admin_session.add(sub)
    admin_session.flush()
    admin_session.add(Score(tenant_id=tid, submission_id=sub.id, score=int(10 * fraction),
                            max_score=10, fraction=fraction, passed=passed,
                            per_item=[], source="auto"))
    admin_session.flush()
    return sub

def test_dashboard_states_filters_and_actions(app_client, admin_session, tenant_a):
    p = _login(app_client, admin_session, tenant_a)
    tid = tenant_a.id
    now = datetime.now(UTC)

    # In-progress course, touched (read ch1, passed act1) → Resume.
    c1, acts1 = _course(admin_session, tid, "prog", "Progress Course")
    _offer(admin_session, tid, p.id, c1.id, name="C1")
    ch1 = admin_session.query(Chapter).filter_by(course_id=c1.id, number=1).one()
    admin_session.add(ChapterRead(tenant_id=tid, person_id=p.id, chapter_id=ch1.id))
    _pass(admin_session, tid, p.id, acts1[0].id)

    # Upcoming course (offering starts next week) → no action, Starts chip.
    c2, _ = _course(admin_session, tid, "up", "Upcoming Course")
    _offer(admin_session, tid, p.id, c2.id, name="C2", starts_at=now + timedelta(days=7))

    # Completed course → Review feedback + certificate eligible.
    c3, acts3 = _course(admin_session, tid, "done", "Finished Course", chapters=1)
    _offer(admin_session, tid, p.id, c3.id, name="C3")
    _pass(admin_session, tid, p.id, acts3[0].id)
    admin_session.add(CourseCompletion(tenant_id=tid, person_id=p.id, course_id=c3.id,
                                       pct=1.0, status="completed",
                                       completed_at=now))
    admin_session.commit()

    r = app_client.get("/", headers=H)
    assert r.status_code == 200
    # States render with counts in the filter bar.
    assert "In progress (1)" in r.text
    assert "Upcoming (1)" in r.text
    assert "Completed (1)" in r.text
    # One action per state.
    assert "Resume" in r.text
    assert "Review feedback" in r.text
    # Card facts: grade, pass mark, last-read, certificate.
    assert "final pass mark 70%" in r.text
    assert "Last read: Ch 1" in r.text
    assert "Get your certificate" in r.text

    # Filtering narrows to one card.
    r = app_client.get("/?filter=completed", headers=H)
    assert "Finished Course" in r.text
    assert "Upcoming Course" not in r.text

def test_start_action_when_never_touched(app_client, admin_session, tenant_a):
    p = _login(app_client, admin_session, tenant_a, email="fresh@a.edu")
    tid = tenant_a.id
    course, _ = _course(admin_session, tid, "new", "Brand New Course")
    _offer(admin_session, tid, p.id, course.id, name="CN")
    admin_session.commit()

    r = app_client.get("/", headers=H)
    assert ">Start<" in r.text
    assert "/courses/new/chapters/1" in r.text

def test_continue_resumes_last_meaningful_touch(app_client, admin_session, tenant_a):
    p = _login(app_client, admin_session, tenant_a, email="resume@a.edu")
    tid = tenant_a.id

    a_course, a_acts = _course(admin_session, tid, "older", "Older Course")
    _offer(admin_session, tid, p.id, a_course.id, name="CA")
    b_course, b_acts = _course(admin_session, tid, "newer", "Newer Course", chapters=3)
    _offer(admin_session, tid, p.id, b_course.id, name="CB")

    # Touch A first (submission), then B (read chapter 2) — B is most recent,
    # chapter 2 is read but its activity unpassed → resume at B chapter 2.
    _pass(admin_session, tid, p.id, a_acts[0].id, fraction=0.2, passed=False)
    ch2 = admin_session.query(Chapter).filter_by(course_id=b_course.id, number=2).one()
    admin_session.add(ChapterRead(tenant_id=tid, person_id=p.id, chapter_id=ch2.id))
    admin_session.commit()

    r = app_client.get("/", headers=H)
    assert "Newer Course &middot; Chapter 2" in r.text or "Newer Course · Chapter 2" in r.text
    assert "/courses/newer/chapters/2" in r.text

def test_progress_page_rich_content(app_client, admin_session, tenant_a):
    p = _login(app_client, admin_session, tenant_a, email="rich@a.edu")
    tid = tenant_a.id
    now = datetime.now(UTC)

    course, acts = _course(admin_session, tid, "deep", "Deep Course")
    off = _offer(admin_session, tid, p.id, course.id, name="CD")
    # Activity 1: passed. Activity 2: overdue (due yesterday, not passed).
    _pass(admin_session, tid, p.id, acts[0].id)
    admin_session.add(OfferingActivity(tenant_id=tid, offering_id=off.id,
                                       activity_id=acts[1].id,
                                       due_at=now - timedelta(days=1)))
    admin_session.commit()

    r = app_client.get("/progress", headers=H)
    assert r.status_code == 200
    # Course and activity names, threshold, attempts, overdue flag.
    assert "Deep Course" in r.text
    assert "Deep Course Test 1" in r.text
    assert "70%" in r.text                      # pass mark column
    assert "1/3" in r.text                      # attempts used / max
    assert "Overdue" in r.text
    assert "Grade" in r.text
