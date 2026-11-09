"""Query-count guard for the learner dashboard / progress page (review item 5).

The per-course fan-out on ``/`` and ``/progress`` was N+1 (~15 queries/course,
100+ at 8 courses). These tests seed a learner in a parametrised number of
courses, render each page, and count executed SQL statements: the count must
stay bounded and NOT scale with course count. If someone reintroduces a
per-course query, the near-flat delta assertion fails.
"""

from __future__ import annotations

from dotmac_kernel.session_runtime import resolve_database_runtime
from sqlalchemy import event

from app.models.assessment import Activity, Score, Submission
from app.models.cohort import Cohort, Enrollment
from app.models.course import Chapter, Course
from app.models.offering import CourseOffering
from app.models.reading import ChapterRead
from app.services.security import hash_password
from legacy_probe.conftest import grant_role, make_person_credential, make_person_party, web_login

H = {"Host": "alpha.localhost"}

def _login(app_client, admin_session, tenant, email):
    person = make_person_party(admin_session, tenant_id=tenant.id, email=email, first_name="Q", last_name="Count")
    admin_session.add(person)
    admin_session.flush()
    admin_session.add(make_person_credential(admin_session, tenant_id=tenant.id, party_id=person.id, email=email, password_hash=hash_password("password1")))
    grant_role(admin_session, tenant_id=tenant.id, party_id=person.id)
    admin_session.commit()
    web_login(app_client, H, email, "password1")
    return person

def _seed_courses(admin_session, tid, person_id, n, prefix="qc"):
    for i in range(n):
        course = Course(tenant_id=tid, slug=f"{prefix}-{i}", title=f"QC {prefix} {i}",
                        discipline="networking", source_ref="x", version=1, status="published", listed=False)
        admin_session.add(course)
        admin_session.flush()
        coh = Cohort(tenant_id=tid, name=f"QC coh {prefix} {i}", discipline="networking", status="active")
        admin_session.add(coh)
        admin_session.flush()
        admin_session.add(Enrollment(tenant_id=tid, cohort_id=coh.id, person_id=person_id,
                                     role_in_cohort="student", status="active"))
        admin_session.add(CourseOffering(tenant_id=tid, cohort_id=coh.id, course_id=course.id,
                                         status="active"))
        for c in range(1, 4):
            ch = Chapter(tenant_id=tid, course_id=course.id, number=c,
                         title=f"Ch{c}", body_html="<p>x</p>")
            admin_session.add(ch)
            act = Activity(tenant_id=tid, course_id=course.id, chapter_number=c,
                           type="mcq_test", title=f"T{c}", pass_threshold=0.7, max_attempts=3, grading="auto", weight=1.0)
            admin_session.add(act)
            admin_session.flush()
            admin_session.add(ChapterRead(tenant_id=tid, person_id=person_id, chapter_id=ch.id))
            sub = Submission(tenant_id=tid, activity_id=act.id, person_id=person_id,
                             answers={}, attempt_no=1)
            admin_session.add(sub)
            admin_session.flush()
            admin_session.add(Score(tenant_id=tid, submission_id=sub.id, score=8, max_score=10,
                                    fraction=0.8, passed=True, per_item=[], source="auto"))
    admin_session.commit()

def _count_queries(app_client, path):
    counter = {"n": 0}

    def _before(conn, cursor, statement, params, context, executemany):
        counter["n"] += 1

    event.listen(resolve_database_runtime().engine, "before_cursor_execute", _before)
    try:
        r = app_client.get(path, headers=H)
        assert r.status_code == 200
    finally:
        event.remove(resolve_database_runtime().engine, "before_cursor_execute", _before)
    return counter["n"]

def test_dashboard_queries_do_not_scale_with_courses(app_client, admin_session, tenant_a):
    p = _login(app_client, admin_session, tenant_a, "qc-dash@a.edu")
    _seed_courses(admin_session, tenant_a.id, p.id, 6)
    n_home = _count_queries(app_client, "/")
    n_prog = _count_queries(app_client, "/progress")
    # Absolute ceilings well under the old ~100+/8-courses fan-out. CI measured 51
    # queries at 6 courses (run 41624421873); that count is a fixed per-request
    # overhead (auth, tenant, nav) on kernel a107, not per-course work, so the
    # ceiling is 55. The flat-delta test below stays unchanged and guards against
    # growth with the number of courses.
    assert n_home < 55, f"/ used {n_home} queries at 6 courses"
    assert n_prog < 55, f"/progress used {n_prog} queries at 6 courses"

def test_dashboard_query_delta_is_near_flat(app_client, admin_session, tenant_a):
    """Adding 3 courses must not add a per-course batch of queries."""
    p = _login(app_client, admin_session, tenant_a, "qc-delta@a.edu")
    _seed_courses(admin_session, tenant_a.id, p.id, 3, prefix="d3")
    home_3 = _count_queries(app_client, "/")
    prog_3 = _count_queries(app_client, "/progress")
    _seed_courses(admin_session, tenant_a.id, p.id, 6, prefix="d9")  # now 9 total
    home_9 = _count_queries(app_client, "/")
    prog_9 = _count_queries(app_client, "/progress")
    # Going 3 -> 9 courses (3x) must stay near-constant; the old per-course N+1
    # (~15 queries/course) would add ~90. Measured residual is ~1 query/course
    # (a tiny unavoidable per-row allowance), so cap the delta well under linear.
    assert home_9 - home_3 <= 10, f"/ grew {home_3}->{home_9} adding 6 courses"
    assert prog_9 - prog_3 <= 10, f"/progress grew {prog_3}->{prog_9} adding 6 courses"
