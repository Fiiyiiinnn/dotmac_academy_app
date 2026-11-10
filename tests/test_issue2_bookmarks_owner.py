# Maintained port of legacy_probe/web/test_bookmarks_owner.py (issue #2 wave 5).
"""Historical bookmark scenarios with genuine canonical account/installed owners.

Hosted only; a004 physical compatibility and installed assembly remain required.
The parent historical tests are preserved unchanged.
"""

from uuid import uuid4

import pytest
from dotmac_learning.curriculum_models import Chapter
from dotmac_learning.models import Course
from dotmac_learning.participation_models import Cohort, CourseOffering, Enrollment
from fastapi import HTTPException

from app.services import bookmarks, catalog
from app.services.web_auth import require_web_user
from app.web import bookmarks as routes
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
from tests.test_issue2_ical_owner import _party, current_account  # noqa: E402

assert current_account


def _request(tenant, token=None):
    request = account_request(tenant, token)
    request.scope.update(
        {
            "scheme": "https",
            "server": ("alpha.localhost", 443),
            "path": "/bookmarks",
            "root_path": "",
            "query_string": b"",
            "method": "GET",
        }
    )
    return request


def seed(a, *, enrolled=True):
    c = Course(
        tenant_id=a.tid,
        slug=uuid4().hex,
        title="Bookmark owner",
        discipline="test",
        source_ref="test",
        status="published",
        listed=False,
    )
    a.admin.add(c)
    a.admin.flush()
    ch = Chapter(tenant_id=a.tid, course_id=c.id, number=1, title="One")
    cohort = Cohort(tenant_id=a.tid, name=uuid4().hex, discipline="test")
    a.admin.add_all([ch, cohort])
    a.admin.flush()
    if enrolled:
        a.admin.add(
            Enrollment(tenant_id=a.tid, cohort_id=cohort.id, person_id=a.pid, role_in_cohort="student", status="active")
        )
    a.admin.add(CourseOffering(tenant_id=a.tid, cohort_id=cohort.id, course_id=c.id, status="active"))
    a.admin.commit()
    return c, ch


def test_resolver_real_lookup_access_order_and_course_binding(current_account, monkeypatch):
    a = current_account
    course, chapter = seed(a)
    req = _request(a.tenant, a.token)
    account = require_web_user(req, a.db)
    root = a.db.get_transaction()
    assert account.id == a.pid
    found = routes._resolve(a.db, a.tid, course.slug, 1, account.id)
    assert (found[0].id, found[1].id) == (course.id, chapter.id)
    assert catalog.catalog_chapter_by_number(a.db, tenant_id=a.tid, course_id=uuid4(), number=1) is None
    assert a.db.get_transaction() is root
    with pytest.raises(HTTPException) as caught:
        routes._resolve(a.db, a.tid, course.slug, 999, a.pid)
    assert caught.value.status_code == 404
    denied, _ = seed(a, enrolled=False)
    # Missing course precedes policy; known but inaccessible course precedes chapter lookup.
    with pytest.raises(HTTPException) as caught:
        routes._resolve(a.db, a.tid, "missing", 999, a.pid)
    assert caught.value.status_code == 404

    def unexpected(*args, **kwargs):
        raise AssertionError("chapter lookup must follow access guard")

    monkeypatch.setattr(routes.catalog, "catalog_chapter_by_number", unexpected)
    with pytest.raises(HTTPException) as caught:
        routes._resolve(a.db, a.tid, denied.slug, 999, a.pid)
    assert caught.value.status_code == 403


def test_actual_toggle_note_escape_partial_keys_and_rollback(current_account):
    a = current_account
    course, chapter = seed(a)
    other = _party(a, a.tid)
    a.admin.commit()
    request = _request(a.tenant, a.token)
    person = require_web_user(request, a.db)
    root = a.db.get_transaction()
    response = routes.bookmark_toggle(course.slug, 1, request, person, a.db)
    assert response.context["bookmarked"] is True
    assert response.context["course"].id == course.id and response.context["chapter"].id == chapter.id
    response = routes.bookmark_toggle(course.slug, 1, request, person, a.db)
    assert response.context["bookmarked"] is False
    response = routes.note_save(course.slug, 1, request, "<script>bad</script>", person, a.db)
    assert response.context["saved"] is True
    assert b"&lt;script&gt;bad&lt;/script&gt;" in response.body and b"<script>bad</script>" not in response.body
    assert (
        bookmarks.get_state(a.db, tenant_id=a.tid, person_id=a.pid, chapter_id=chapter.id)["note"].body
        == "<script>bad</script>"
    )
    assert bookmarks.get_state(a.db, tenant_id=a.tid, person_id=other.id, chapter_id=chapter.id)["note"] is None
    assert bookmarks.get_state(a.db, tenant_id=uuid4(), person_id=a.pid, chapter_id=chapter.id)["note"] is None
    response = routes.note_save(course.slug, 1, request, "  ", person, a.db)
    assert response.context["note"] is None
    assert a.db.get_transaction() is root
    a.db.rollback()
    assert bookmarks.get_state(a.db, tenant_id=a.tid, person_id=a.pid, chapter_id=chapter.id) == {
        "bookmarked": False,
        "note": None,
    }


def test_chapter_page_server_renders_bookmark_and_note(current_account):
    """The chapter page arrives with bookmark/note already rendered —
    no load-time htmx fetches for state the server already knows."""
    from app.web import learn as learn_routes

    a = current_account
    course, chapter = seed(a)
    chapter.body_html = "<h2>Intro</h2><p>Read me.</p>"
    a.admin.commit()
    bookmarks.toggle_bookmark(a.db, tenant_id=a.tid, person_id=a.pid, course_id=course.id, chapter_id=chapter.id)
    bookmarks.save_note(
        a.db,
        tenant_id=a.tid,
        person_id=a.pid,
        course_id=course.id,
        chapter_id=chapter.id,
        body="remember this",
    )
    request = _request(a.tenant, a.token)
    person = require_web_user(request, a.db)
    response = learn_routes.chapter(course.slug, 1, request, person, a.db)
    body = response.body.decode()
    assert response.context["bookmarked"] is True
    assert "Bookmarked" in body
    assert "remember this" in body
    assert 'hx-get="/bookmarks/chapters/' not in body
