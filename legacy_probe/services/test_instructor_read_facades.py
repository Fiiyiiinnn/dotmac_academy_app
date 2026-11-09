"""Hosted genuine Learning calculations through Academy route/template adapters.

Authored, not run. Canonical owner fixtures, a004 physical FK compatibility,
Learning successor and whole instructor factory closure are prerequisites. The
separate lifecycle writer gap is not replaced or hidden by these read canaries.
"""
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from dotmac_kernel.models import PartyRoleGrant, Role
from sqlalchemy import event, select, text

from app.models.assessment import Activity
from app.models.cohort import Cohort, Enrollment
from app.models.completion import CourseCompletion
from app.models.course import Course
from app.models.offering import CourseOffering
from app.services import settings_store
from app.services.analytics import item_analysis
from app.services.dashboards import cohort_overview
from app.services.exceptions import NotFoundError
from app.services.lookups import cohort_or_404
from app.services.web_auth import require_web_user
from legacy_probe.owner_helpers import person as _person
from legacy_probe.services.test_analytics_owner import _score
from legacy_probe.web.test_bookmarks_owner import _request
from legacy_probe.web.test_reports_access_policy import _restore_settings

assert _restore_settings


def _seed(a, foreign_tid):
    db, tid = a.admin, a.tid
    now = datetime.now(UTC)
    courses = [Course(tenant_id=tid, slug=uuid4().hex, title=title, discipline="test",
                      source_ref="test", version=1, status="published", listed=False) for title in ("Offered A", "Offered B", "Unrelated")]
    cohort = Cohort(tenant_id=tid, name="Read owner cohort", discipline="test")
    foreign = Cohort(tenant_id=foreign_tid, name="Foreign hidden cohort", discipline="test")
    db.add_all([*courses, cohort, foreign])
    db.flush()
    weak = _person(db, tid, "weak-read@a.edu", " Wea", "K ")
    strong = _person(db, tid, "strong-read@a.edu", "Str", "Ong")
    # Legacy read deliberately does not require an Academy profile or active Party.
    weak.is_active = False
    for p in (weak, strong):
        db.add(Enrollment(tenant_id=tid, cohort_id=cohort.id, person_id=p.id,
                          role_in_cohort="student", status="active"))
    for c in courses[:2]:
        db.add(CourseOffering(tenant_id=tid, cohort_id=cohort.id, course_id=c.id, status="active"))
    for p, pct in ((weak, 0.8), (strong, 1.0)):
        db.add(CourseCompletion(tenant_id=tid, person_id=p.id, course_id=courses[0].id,
                                status="in_progress", pct=pct))
    activities = [Activity(tenant_id=tid, course_id=c.id, chapter_number=1, type="mcq_test",
                           title=c.title, pass_threshold=0.73, grading="auto", weight=1.0) for c in courses]
    db.add_all(activities)
    db.flush()
    _score(db, tid, activities[0].id, weak.id,
           [{"id": "q2", "correct": True}, {"id": "q1", "correct": False}], 0.8)
    _score(db, tid, activities[0].id, weak.id, [{"id": "q2", "correct": False}], 0.2, attempt_no=2)
    _score(db, tid, activities[0].id, strong.id,
           [{"id": "q2", "correct": False}, {"id": "q1", "correct": True}], 0.6)
    from app.models.assessment import Score, Submission
    for person, stamp in ((weak, now - timedelta(days=4)), (strong, now)):
        scores = db.query(Score).join(Submission, Submission.id == Score.submission_id).filter(
            Submission.person_id == person.id, Score.tenant_id == tid).all()
        for score in scores:
            score.created_at = stamp
    # Recent unrelated-course activity must not mask this cohort's stale learner.
    _score(db, tid, activities[2].id, weak.id, [{"id": "outside", "correct": True}], 1.0)
    settings_store.set_many(db, {"learning_instructor_at_risk_fraction": "0.75",
                                 "learning_instructor_stale_days": "3"})
    db.commit()
    return cohort.id, foreign.id, activities[0].id, weak.id, strong.id, now


def test_real_owner_calculations_custom_settings_and_read_transaction(current_account, tenant_b):
    a = current_account
    cid, foreign, aid, weak, strong, now = _seed(a, tenant_b.id)
    db = a.db
    root = db.get_transaction()
    if root is None:
        db.scalar(text("SELECT 1"))
        root = db.get_transaction()
    statements = []
    connection = db.connection()

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(connection, "before_cursor_execute", capture)
    try:
        items = item_analysis(db, tenant_id=a.tid, activity_id=aid)
        assert items == [
            {"id": "q2", "responses": 2, "correct": 1, "p_value": 0.5},
            {"id": "q1", "responses": 2, "correct": 1, "p_value": 0.5},
        ]
        ov = cohort_overview(db, tenant_id=a.tid, cohort_id=cid, now=now)
        assert ov["cohort"].id == cid
        assert [r["person_id"] for r in ov["rows"]] == [weak, strong]
        assert [r["completion_pct"] for r in ov["rows"]] == [0.4, 0.5]
        assert ov["rows"][0]["name"] == "Wea K"
        assert [r["at_risk"] for r in ov["rows"]] == [True, False]
        assert ov["rows"][0]["last_activity_at"] == now - timedelta(days=4)
        # Explicit zero overrides remain meaningful; there is no truthy fallback.
        assert not any(r["at_risk"] for r in cohort_overview(
            db, tenant_id=a.tid, cohort_id=cid, now=now, at_risk_pct=0.0, stale_days=0)["rows"])
        assert not any(r["at_risk"] for r in cohort_overview(
            db, tenant_id=a.tid, cohort_id=cid, now=now, stale_days=5)["rows"])
        assert cohort_or_404(db, tenant_id=a.tid, cohort_id=cid).name == "Read owner cohort"
        for hidden in (foreign, uuid4()):
            with pytest.raises(NotFoundError, match="cohort not found for tenant"):
                cohort_or_404(db, tenant_id=a.tid, cohort_id=hidden)
            with pytest.raises(NotFoundError, match="cohort not found for tenant"):
                cohort_overview(db, tenant_id=a.tid, cohort_id=hidden, now=now)
        assert item_analysis(db, tenant_id=tenant_b.id, activity_id=aid) == []
    finally:
        event.remove(connection, "before_cursor_execute", capture)
    assert statements and all(s.lstrip().upper().startswith("SELECT") for s in statements)
    assert db.get_transaction() is root and not db.new and not db.dirty and not db.deleted
    assert db.scalar(text("SELECT current_user")) == "app_user"
    assert db.scalar(text("SELECT current_setting('app.current_tenant')")) == str(a.tid)


def test_actual_instructor_routes_render_owner_results_and_cohort_404(current_account, tenant_b):
    from fastapi import HTTPException

    from app.web import instructor as routes

    a = current_account
    cid, foreign, aid, _, _, _ = _seed(a, tenant_b.id)
    role = a.admin.scalars(select(Role).where(Role.tenant_id == a.tid, Role.slug == "instructor")).first()
    if role is None:
        role = Role(tenant_id=a.tid, slug="instructor", name="instructor")
        a.admin.add(role)
        a.admin.flush()
    a.admin.add(PartyRoleGrant(tenant_id=a.tid, party_id=a.pid, role_id=role.id))
    a.admin.commit()
    request = _request(a.tenant, a.token)
    person = require_web_user(request, a.db)
    dashboard = routes.cohort_dashboard(cid, request, person=person, db=a.db)
    assert {"request", "cohort", "rows"}.issubset(dashboard.context)
    assert dashboard.context["cohort"].id == cid and len(dashboard.context["rows"]) == 2
    assert b"Read owner cohort" in dashboard.body and b"40%" in dashboard.body
    assert b"At risk" in dashboard.body and b"On track" in dashboard.body
    analysis = routes.item_analytics(aid, request, person=person, db=a.db)
    assert {"request", "activity", "items"}.issubset(analysis.context)
    assert analysis.context["activity"].pass_threshold == 0.73
    assert len(analysis.context["items"]) == 2
    assert b"0.50" in analysis.body and b"Offered A" in analysis.body
    for hidden in (foreign, uuid4()):
        with pytest.raises(NotFoundError):
            routes.cohort_dashboard(hidden, request, person=person, db=a.db)
    with pytest.raises(HTTPException) as caught:
        routes.item_analytics(uuid4(), request, person=person, db=a.db)
    assert caught.value.status_code == 404


def test_changed_stored_threshold_and_window_each_change_real_owner_result(current_account, tenant_b):
    a = current_account
    cid, _, _, weak, _, now = _seed(a, tenant_b.id)

    def risk():
        rows = cohort_overview(a.admin, tenant_id=a.tid, cohort_id=cid, now=now)["rows"]
        return next(row["at_risk"] for row in rows if row["person_id"] == weak)

    assert risk() is True
    settings_store.set_many(a.admin, {"learning_instructor_at_risk_fraction": "0.4"})
    assert risk() is False  # Equality is not below the configured threshold.
    settings_store.set_many(a.admin, {"learning_instructor_at_risk_fraction": "0.41",
                                      "learning_instructor_stale_days": "4"})
    assert risk() is False  # Activity exactly at the stale boundary is not stale.
    settings_store.set_many(a.admin, {"learning_instructor_stale_days": "3"})
    assert risk() is True
