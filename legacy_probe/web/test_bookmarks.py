"""Bookmarks & personal notes (roadmap P1 item 11).

Pins: toggle idempotence, note create/update/delete-on-empty, /bookmarks
listing scoped to the person and their entitled courses, cross-person
isolation, and the unauthenticated redirect.
"""

from __future__ import annotations

from app.models.bookmark import ChapterBookmark, ChapterNote
from app.models.cohort import Cohort, Enrollment
from app.models.course import Chapter, Course
from app.models.offering import CourseOffering
from app.services.security import hash_password
from legacy_probe.conftest import grant_role, make_person_credential, make_person_party, web_login

H = {"Host": "alpha.localhost"}

def _post(app_client, url, data=None):
    """POST with the double-submit CSRF header (cookie set by any prior GET)."""
    if not app_client.cookies.get("csrf_token"):
        app_client.get("/login", headers=H)
    csrf = app_client.cookies.get("csrf_token", "")
    return app_client.post(url, headers={**H, "x-csrf-token": csrf}, data=data)

def _login(app_client, admin_session, tenant, email):
    p = make_person_party(admin_session, tenant_id=tenant.id, email=email, first_name="Bee", last_name="Marks")
    admin_session.add(p)
    admin_session.flush()
    admin_session.add(
        make_person_credential(admin_session, tenant_id=tenant.id, party_id=p.id, email=email, password_hash=hash_password("password1"))
    )
    grant_role(admin_session, tenant_id=tenant.id, party_id=p.id, role="student")
    admin_session.commit()
    web_login(app_client, H, email, "password1")
    return p

def _seed_course(admin_session, tenant, person, slug="bm-course", enrol=True):
    c = Course(tenant_id=tenant.id, slug=slug, title=f"Course {slug}",
               discipline="networking", source_ref="x", version=1, status="published", listed=False)
    admin_session.add(c)
    admin_session.flush()
    ch = Chapter(tenant_id=tenant.id, course_id=c.id, number=1,
                 title="Chapter One", body_html="<p>hello</p>")
    admin_session.add(ch)
    coh = Cohort(tenant_id=tenant.id, name=f"Cohort {slug}", discipline="networking")
    admin_session.add(coh)
    admin_session.flush()
    if enrol:
        admin_session.add(Enrollment(tenant_id=tenant.id, cohort_id=coh.id,
                                     person_id=person.id, role_in_cohort="student",
                                     status="active"))
    admin_session.add(CourseOffering(tenant_id=tenant.id, cohort_id=coh.id,
                                     course_id=c.id, status="active"))
    admin_session.commit()
    return c, ch

def test_bookmarks_requires_login(app_client, tenant_a):
    r = app_client.get("/bookmarks", headers=H, follow_redirects=False)
    assert r.status_code in (302, 303)
    assert "/login" in r.headers.get("location", "")

def test_toggle_is_idempotent_flip(app_client, admin_session, tenant_a):
    person = _login(app_client, admin_session, tenant_a, "bm1@a.edu")
    course, chapter = _seed_course(admin_session, tenant_a, person, slug="bm-flip")

    url = f"/bookmarks/chapters/{course.slug}/1/toggle"
    r = _post(app_client, url)
    assert r.status_code == 200 and "Bookmarked" in r.text
    r = _post(app_client, url)
    assert r.status_code == 200 and "Bookmarked" not in r.text

    admin_session.expire_all()
    count = admin_session.query(ChapterBookmark).filter_by(
        tenant_id=tenant_a.id, person_id=person.id, chapter_id=chapter.id).count()
    assert count == 0

def test_note_create_update_and_delete_on_empty(app_client, admin_session, tenant_a):
    person = _login(app_client, admin_session, tenant_a, "bm2@a.edu")
    course, chapter = _seed_course(admin_session, tenant_a, person, slug="bm-note")

    url = f"/bookmarks/chapters/{course.slug}/1/note"
    r = _post(app_client, url, data={"body": "splice loss < 0.1dB"})
    assert r.status_code == 200 and "Saved" in r.text and "splice loss" in r.text

    r = _post(app_client, url, data={"body": "updated wisdom"})
    assert "updated wisdom" in r.text
    admin_session.expire_all()
    note = admin_session.query(ChapterNote).filter_by(
        tenant_id=tenant_a.id, person_id=person.id, chapter_id=chapter.id).one()
    assert note.body == "updated wisdom"

    r = _post(app_client, url, data={"body": "   "})
    assert r.status_code == 200
    admin_session.expire_all()
    assert admin_session.query(ChapterNote).filter_by(
        tenant_id=tenant_a.id, person_id=person.id, chapter_id=chapter.id).count() == 0

def test_note_body_rendered_escaped(app_client, admin_session, tenant_a):
    person = _login(app_client, admin_session, tenant_a, "bm3@a.edu")
    course, _ = _seed_course(admin_session, tenant_a, person, slug="bm-xss")
    url = f"/bookmarks/chapters/{course.slug}/1/note"
    r = _post(app_client, url, data={"body": '<script>alert(1)</script>'})
    assert "<script>alert(1)</script>" not in r.text
    assert "&lt;script&gt;" in r.text

def test_unentitled_course_is_forbidden(app_client, admin_session, tenant_a):
    person = _login(app_client, admin_session, tenant_a, "bm4@a.edu")
    course, _ = _seed_course(admin_session, tenant_a, person, slug="bm-nope", enrol=False)
    r = _post(app_client, f"/bookmarks/chapters/{course.slug}/1/toggle")
    assert r.status_code in (403, 404)

def test_listing_scoped_to_person_and_entitlement(app_client, admin_session, tenant_a):
    other = make_person_party(admin_session, tenant_id=tenant_a.id, email="bm-other@a.edu", first_name="O", last_name="P")
    admin_session.add(other)
    admin_session.flush()

    person = _login(app_client, admin_session, tenant_a, "bm5@a.edu")
    course, chapter = _seed_course(admin_session, tenant_a, person, slug="bm-list")

    _post(app_client, f"/bookmarks/chapters/{course.slug}/1/toggle")
    _post(app_client, f"/bookmarks/chapters/{course.slug}/1/note",
          data={"body": "mine alone"})
    # Another person's note on the same chapter must never appear.
    admin_session.add(ChapterNote(tenant_id=tenant_a.id, person_id=other.id,
                                  course_id=course.id, chapter_id=chapter.id,
                                  body="other persons secret"))
    admin_session.commit()

    r = app_client.get("/bookmarks", headers=H)
    assert r.status_code == 200
    assert "Chapter 1" in r.text and "mine alone" in r.text
    assert "Bookmarked" in r.text
    assert "other persons secret" not in r.text
