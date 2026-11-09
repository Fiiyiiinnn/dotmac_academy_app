# Maintained port of legacy_probe/web/test_todo_calendar_owner.py (issue #2 wave 5).
"""Historical todo/feed scenarios using current accounts and real owners.

Hosted installed assembly and physical Learning/Party FK transition required.
"""

from datetime import UTC, datetime, timedelta

import pytest
from dotmac_kernel.session_runtime import resolve_database_runtime
from dotmac_learning.delivery_models import ClassSession, OfferingActivity
from dotmac_learning.models import Activity
from dotmac_learning.participation_models import CourseOffering
from fastapi import HTTPException
from sqlalchemy import select

from app.models.account_token import AccountToken
from app.services import ical, settings_store, todo
from app.services.security import hash_token
from app.services.web_auth import require_web_user
from app.web import calendar_feed
from app.web import todo as routes
from tests.test_issue2_ical_owner import _request as account_request
from tests.test_issue2_ical_owner import (
    admin_session,
    app_client,
    app_user_session,
    sign_in_account,
    tenant_a,
    tenant_b,
)

assert admin_session and app_client and app_user_session and sign_in_account and tenant_a and tenant_b
from tests.test_issue2_bookmarks_owner import seed  # noqa: E402
from tests.test_issue2_ical_owner import calendar_account, current_account  # noqa: E402

assert calendar_account and current_account


def _request(tenant, token=None):
    request = account_request(tenant, token)
    request.scope.update(
        {
            "scheme": "https",
            "server": ("alpha.localhost", 443),
            "path": "/todo",
            "root_path": "",
            "query_string": b"",
            "method": "GET",
        }
    )
    return request


def test_todo_genuine_session_rotation_once_and_feed_token_only(calendar_account, tenant_b):
    a = calendar_account
    request = _request(a.tenant, a.token)
    person = require_web_user(request, a.db)
    response = routes.rotate_feed_token(request, person, a.db)
    raw = response.context["new_feed_token"]
    assert raw and response.context["has_feed_token"] is True
    assert response.headers["Cache-Control"] == "private, no-store"
    assert routes.todo_page(request, person, a.db).context["new_feed_token"] is None
    assert hash_token(raw).encode() not in response.body
    # Even an authenticated cookie cannot replace a missing/invalid feed token.
    assert calendar_feed.calendar_feed(request, "", a.db).status_code == 404
    assert calendar_feed.calendar_feed(request, "bad", a.db).status_code == 404
    assert calendar_feed.calendar_feed(_request(tenant_b, a.token), raw, a.db).status_code == 404
    settings_store.set_many(a.admin, {"branding_name": "Custom 学 Calendar", "ical_feed_filename": "custom.ics"})
    a.admin.commit()
    feed = calendar_feed.calendar_feed(request, raw, a.db)
    assert feed.status_code == 200
    assert feed.headers["Content-Disposition"] == 'attachment; filename="custom.ics"'
    assert feed.headers["Cache-Control"] == "private, no-store"
    assert b"X-WR-CALNAME:Custom" in feed.body and "学".encode() in feed.body
    assert raw.encode() not in feed.body and hash_token(raw).encode() not in feed.body
    rotated = routes.rotate_feed_token(request, person, a.db).context["new_feed_token"]
    assert calendar_feed.calendar_feed(request, raw, a.db).status_code == 404
    assert calendar_feed.calendar_feed(request, rotated, a.db).status_code == 200


def test_real_timeline_buckets_sessions_uid_policy_and_other_account(calendar_account):
    a = calendar_account
    course, _ = seed(a)
    offering = a.admin.scalars(select(CourseOffering).where(CourseOffering.course_id == course.id)).one()
    now = datetime.now(UTC)
    activities = []
    for number, days in enumerate((-2, 2, 10), 1):
        activity = Activity(
            tenant_id=a.tid,
            course_id=course.id,
            chapter_number=number,
            type="mcq_test",
            title=f"Due {number}",
            pass_threshold=0.6,
            grading="auto",
            weight=1.0,
        )
        a.admin.add(activity)
        a.admin.flush()
        a.admin.add(
            OfferingActivity(
                tenant_id=a.tid, offering_id=offering.id, activity_id=activity.id, due_at=now + timedelta(days=days)
            )
        )
        activities.append(activity)
    session = ClassSession(
        tenant_id=a.tid,
        cohort_id=offering.cohort_id,
        offering_id=offering.id,
        title="Calendar session",
        session_type=settings_store.effective(a.db, require_store=True)["learning_default_session_type"],
        starts_at=now + timedelta(hours=2),
        ends_at=now + timedelta(hours=3),
    )
    a.admin.add(session)
    a.admin.commit()
    settings_store.set_many(
        a.admin,
        {
            "ical_feed_uid_domain": "calendar.example.test",
            "learning_todo_near_deadline_days": "3",
            "learning_todo_far_deadline_days": "15",
        },
    )
    a.admin.commit()
    timeline = todo.timeline_for_person(a.db, tenant_id=a.tid, person_id=a.pid, now=now)
    assert timeline["near_deadline_days"] == 3 and timeline["far_deadline_days"] == 15
    assert [r["activity"].id for r in timeline["overdue"]] == [activities[0].id]
    assert [r["activity"].id for r in timeline["next_7"]] == [activities[1].id]
    assert [r["activity"].id for r in timeline["next_30"]] == [activities[2].id]
    events = ical.events_for_person(a.db, tenant_id=a.tid, person_id=a.pid, now=now)
    assert {e["uid"] for e in events} == {f"due-{v.id}@calendar.example.test" for v in activities} | {
        f"session-{session.id}@calendar.example.test"
    }
    assert all(e["starts_at"] is not None for e in events)
    # Changing GUC cannot resolve this tenant's token in the other tenant.
    raw = ical.mint_feed_token(a.db, tenant_id=a.tid, person_id=a.pid)
    token = a.db.scalars(select(AccountToken).where(AccountToken.token_hash == hash_token(raw))).one()
    token.kind = "other-purpose"
    a.db.flush()
    assert ical.resolve_feed_token(a.db, tenant_id=a.tid, raw=raw) is None
    a.db.rollback()
    resolve_database_runtime().set_tenant(a.db, a.tid)


def test_todo_and_bookmark_missing_session_use_real_guard(calendar_account):
    a = calendar_account
    with pytest.raises(HTTPException) as caught:
        require_web_user(_request(a.tenant), a.db)
    assert caught.value.status_code == 303
