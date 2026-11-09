"""Tests for the student portal — Task 11 (htmx)."""

from __future__ import annotations

import uuid

import pytest

from app.models.assessment import Activity, Question, QuestionBank
from app.models.cohort import Cohort, Enrollment
from app.models.course import Chapter, Course
from app.models.offering import CourseOffering
from app.services.security import hash_password
from legacy_probe.conftest import grant_role, make_person_credential, make_person_party, web_login


def _login(app_client, admin_session, tenant):
    p = make_person_party(admin_session, tenant_id=tenant.id, email="s@a.edu", first_name="S", last_name="L")
    admin_session.add(p)
    admin_session.flush()
    admin_session.add(
        make_person_credential(admin_session, tenant_id=tenant.id, party_id=p.id, email="s@a.edu", password_hash=hash_password("password1"))
    )
    grant_role(admin_session, tenant_id=tenant.id, party_id=p.id)
    admin_session.commit()
    h = {"Host": "alpha.localhost"}
    # Log in via the CSRF double-submit flow (web_login).
    web_login(app_client, h, "s@a.edu", "password1")
    return p, h

def test_take_test_flow(app_client, admin_session, tenant_a):
    p, h = _login(app_client, admin_session, tenant_a)

    # Seed Course + Chapter + QuestionBank + Question + Activity.
    c = Course(
        tenant_id=tenant_a.id,
        slug="foundation",
        title="F",
        discipline="networking",
        source_ref="x",
        version=1,
        status="published", listed=False,
    )
    admin_session.add(c)
    admin_session.flush()
    admin_session.add(
        Chapter(
            tenant_id=tenant_a.id,
            course_id=c.id,
            number=3,
            title="Three",
            part="II",
            body_html="<p>body</p>",
            source_hash="h",
            order_index=3,
        )
    )
    bank = QuestionBank(
        tenant_id=tenant_a.id,
        course_id=c.id,
        chapter_number=3,
        kind="chapter",
        version=1,
    )
    admin_session.add(bank)
    admin_session.flush()
    admin_session.add(
        Question(
            tenant_id=tenant_a.id,
            bank_id=bank.id,
            ext_id="q1",
            stem="Pick A",
            type="single",
            options=["A", "B"],
            correct=["A"],
            rubric_category="recall",
            explanation="Because A",
            weight=1,
        )
    )
    act = Activity(
        tenant_id=tenant_a.id,
        course_id=c.id,
        chapter_number=3,
        type="mcq_test",
        bank_id=bank.id,
        title="Ch3",
        pass_threshold=0.6, grading="auto", weight=1.0,
    )
    admin_session.add(act)
    admin_session.flush()
    # Entitle the learner: cohort + enrollment + offering for this course.
    coh = Cohort(tenant_id=tenant_a.id, name="C", discipline="networking", status="active")
    admin_session.add(coh)
    admin_session.flush()
    admin_session.add(
        Enrollment(tenant_id=tenant_a.id, cohort_id=coh.id, person_id=p.id, role_in_cohort="student", status="active")
    )
    admin_session.add(CourseOffering(tenant_id=tenant_a.id, cohort_id=coh.id, course_id=c.id, status="active"))
    admin_session.commit()

    # GET chapter page — response sets the csrf_token cookie in the TestClient jar.
    r_ch = app_client.get("/courses/foundation/chapters/3", headers=h)
    assert r_ch.status_code == 200
    assert 'class="shell-layout shell-layout-full"' in r_ch.text
    assert 'class="shell-footer shell-footer-full ' in r_ch.text

    # Extract csrf token — TestClient (httpx-based) stores cookies from responses.
    csrf = app_client.cookies.get("csrf_token") or r_ch.cookies.get("csrf_token", "")

    # POST submit WITH the x-csrf-token header (required because the client now carries
    # both the session cookie and the csrf_token cookie, so CSRF middleware enforces it).
    r = app_client.post(
        f"/activities/{act.id}/submit",
        headers={**h, "x-csrf-token": csrf},
        data={"q1": "A"},
    )
    assert r.status_code == 200
    assert "Passed" in r.text
    assert "Because A" in r.text

def test_cross_tenant_isolation(app_client, admin_session, tenant_a, tenant_b):
    _login(app_client, admin_session, tenant_a)
    h = {"Host": "alpha.localhost"}

    # Seed Course + QuestionBank + Activity under tenant_b only.
    c_b = Course(
        tenant_id=tenant_b.id,
        slug="foundation",
        title="F",
        discipline="networking",
        source_ref="x",
        version=1,
        status="published", listed=False,
    )
    admin_session.add(c_b)
    admin_session.flush()
    bank_b = QuestionBank(
        tenant_id=tenant_b.id,
        course_id=c_b.id,
        chapter_number=1,
        kind="chapter",
        version=1,
    )
    admin_session.add(bank_b)
    admin_session.flush()
    act_b = Activity(
        tenant_id=tenant_b.id,
        course_id=c_b.id,
        chapter_number=1,
        type="mcq_test",
        bank_id=bank_b.id,
        title="B-Act",
        pass_threshold=0.6, grading="auto", weight=1.0,
    )
    admin_session.add(act_b)
    admin_session.commit()

    # tenant_a user must NOT be able to access tenant_b's activity.
    r = app_client.get(f"/activities/{act_b.id}", headers=h)
    assert r.status_code == 404

    # Nonexistent chapter (no foundation course in tenant_a) → 404.
    r2 = app_client.get("/courses/foundation/chapters/999", headers=h)
    assert r2.status_code == 404

def test_dashboard_lists_multiple_courses(app_client, admin_session, tenant_a):
    """The Learn home lists every course the student is enrolled in (by discipline)."""
    from app.models.cohort import Cohort, Enrollment

    p, h = _login(app_client, admin_session, tenant_a)
    courses = []
    for slug, title in (("foundation", "Network Foundation"), ("fiber-engineering", "Fiber Engineering")):
        c = Course(tenant_id=tenant_a.id, slug=slug, title=title, discipline="networking", source_ref="x", version=1, status="published", listed=False)
        admin_session.add(c)
        admin_session.flush()
        admin_session.add(
            Chapter(
                tenant_id=tenant_a.id,
                course_id=c.id,
                number=1,
                title=f"{title} ch1",
                part="I",
                body_html="<p>x</p>",
                source_hash="h",
                order_index=1,
            )
        )
        courses.append(c)
    coh = Cohort(tenant_id=tenant_a.id, name="Abuja", discipline="networking", status="active")
    admin_session.add(coh)
    admin_session.flush()
    admin_session.add(
        Enrollment(tenant_id=tenant_a.id, cohort_id=coh.id, person_id=p.id, role_in_cohort="student", status="active")
    )
    for c in courses:
        admin_session.add(CourseOffering(tenant_id=tenant_a.id, cohort_id=coh.id, course_id=c.id, status="active"))
    admin_session.commit()
    try:
        r = app_client.get("/", headers=h)
        assert r.status_code == 200
        assert "Network Foundation" in r.text and "Fiber Engineering" in r.text
        assert "/courses/fiber-engineering/chapters/1" in r.text
    finally:
        # Clean up committed rows (chapters cascade from courses) so this test
        # leaves no residue in the shared test DB.
        admin_session.query(Course).filter(Course.tenant_id == tenant_a.id).delete()
        admin_session.commit()

def test_chapter_lists_multiple_activities(app_client, admin_session, tenant_a):
    p, h = _login(app_client, admin_session, tenant_a)
    slug = f"multi-activity-{uuid.uuid4().hex[:8]}"
    c = Course(
        tenant_id=tenant_a.id,
        slug=slug,
        title="Multi Activity",
        discipline="networking",
        source_ref="x",
        version=1,
        status="published", listed=False,
    )
    admin_session.add(c)
    admin_session.flush()
    admin_session.add(
        Chapter(
            tenant_id=tenant_a.id,
            course_id=c.id,
            number=14,
            title="Fourteen",
            part="II",
            body_html="<h2>Read this</h2><p>body</p>",
            source_hash="h",
            order_index=14,
        )
    )
    lab = Activity(
        tenant_id=tenant_a.id,
        course_id=c.id,
        chapter_number=14,
        type="lab",
        title="Router lab",
        pass_threshold=0.7, grading="auto", weight=1.0,
    )
    test = Activity(
        tenant_id=tenant_a.id,
        course_id=c.id,
        chapter_number=14,
        type="mcq_test",
        title="Chapter 14 test",
        pass_threshold=0.0, grading="auto", weight=1.0,
    )
    admin_session.add_all([lab, test])
    coh = Cohort(tenant_id=tenant_a.id, name="C", discipline="networking", status="active")
    admin_session.add(coh)
    admin_session.flush()
    admin_session.add(
        Enrollment(tenant_id=tenant_a.id, cohort_id=coh.id, person_id=p.id, role_in_cohort="student", status="active")
    )
    admin_session.add(CourseOffering(tenant_id=tenant_a.id, cohort_id=coh.id, course_id=c.id, status="active"))
    admin_session.commit()
    try:
        r = app_client.get(f"/courses/{slug}/chapters/14", headers=h)
        assert r.status_code == 200
        assert "Chapter activities" in r.text
        assert "Chapter 14 test" in r.text
        assert "Router lab" in r.text
        assert f"/activities/{test.id}" in r.text
        assert f"/labs/{lab.id}" in r.text
        assert 'id="chapter-test-link"' in r.text
    finally:
        admin_session.query(Course).filter(Course.tenant_id == tenant_a.id, Course.slug == slug).delete()
        admin_session.commit()

# ---------------------------------------------------------------------------
# Subtopic progress is server state, not browser state
# ---------------------------------------------------------------------------

def _seed_entitled_chapter(admin_session, tenant, person, *, slug, body_html):
    """A course the learner may study, with one chapter and one activity."""
    c = Course(tenant_id=tenant.id, slug=slug, title="SP", discipline="networking", source_ref="x", version=1, status="published", listed=False)
    admin_session.add(c)
    admin_session.flush()
    admin_session.add(
        Chapter(
            tenant_id=tenant.id,
            course_id=c.id,
            number=1,
            title="One",
            part="I",
            body_html=body_html,
            source_hash="sh",
            order_index=1,
        )
    )
    bank = QuestionBank(tenant_id=tenant.id, course_id=c.id, chapter_number=1, kind="chapter", version=1)
    admin_session.add(bank)
    admin_session.flush()
    admin_session.add(
        Question(
            tenant_id=tenant.id,
            bank_id=bank.id,
            ext_id="q1",
            stem="Pick A",
            type="single",
            options=["A", "B"],
            correct=["A"],
            rubric_category="recall",
            explanation="",
            weight=1,
        )
    )
    admin_session.add(
        Activity(
            tenant_id=tenant.id,
            course_id=c.id,
            chapter_number=1,
            type="mcq_test",
            bank_id=bank.id,
            title="Ch1 test",
            pass_threshold=0.6, grading="auto", weight=1.0,
        )
    )
    coh = Cohort(tenant_id=tenant.id, name=f"C-{slug}", discipline="networking", status="active")
    admin_session.add(coh)
    admin_session.flush()
    admin_session.add(
        Enrollment(
            tenant_id=tenant.id, cohort_id=coh.id, person_id=person.id, role_in_cohort="student", status="active"
        )
    )
    admin_session.add(CourseOffering(tenant_id=tenant.id, cohort_id=coh.id, course_id=c.id, status="active"))
    admin_session.commit()
    return c

def test_subtopic_progress_survives_a_new_browser_session(app_client, admin_session, tenant_a):
    """The bug this replaced: progress lived in window.localStorage, so a learner
    who switched device found the chapter's activities re-locked with nothing on
    the server to show the work was done. A fresh cookie jar is that new device."""
    p, h = _login(app_client, admin_session, tenant_a)
    _seed_entitled_chapter(admin_session, tenant_a, p, slug="subtopics", body_html="<h2>Alpha</h2><h2>Beta</h2>")

    r = app_client.get("/courses/subtopics/chapters/1", headers=h)
    assert r.status_code == 200
    # Nothing done yet: the activity link is rendered locked, server-side.
    assert 'class="chapter-activity-link btn-ghost justify-between gap-3 action-disabled"' in r.text
    assert 'data-subtopics-complete="false"' in r.text

    csrf = app_client.cookies.get("csrf_token") or ""
    hh = {**h, "x-csrf-token": csrf}
    for slug in ("alpha", "beta"):
        rp = app_client.post(f"/courses/subtopics/chapters/1/subtopics/{slug}/complete", headers=hh)
        assert rp.status_code == 200, rp.text
    assert rp.json()["chapter_complete"] is True

    # A brand-new client — no cookies, no localStorage, a different device.
    app_client.cookies.clear()
    web_login(app_client, h, "s@a.edu", "password1")
    r2 = app_client.get("/courses/subtopics/chapters/1", headers=h)
    assert r2.status_code == 200
    assert 'data-subtopics-complete="true"' in r2.text
    assert 'data-subtopic-done="true"' in r2.text
    # ...and the activity link now arrives unlocked, without JavaScript running.
    assert "chapter-activity-link" in r2.text
    assert 'class="chapter-activity-link btn-ghost justify-between gap-3 action-disabled"' not in r2.text

def test_unknown_subtopic_slug_is_rejected(app_client, admin_session, tenant_a):
    """The slug is content-derived and arrives from the client, so it is checked
    against the chapter's real headings rather than stored on trust."""
    p, h = _login(app_client, admin_session, tenant_a)
    _seed_entitled_chapter(admin_session, tenant_a, p, slug="subtopics2", body_html="<h2>Alpha</h2>")

    app_client.get("/courses/subtopics2/chapters/1", headers=h)
    hh = {**h, "x-csrf-token": app_client.cookies.get("csrf_token") or ""}
    r = app_client.post("/courses/subtopics2/chapters/1/subtopics/not-a-heading/complete", headers=hh)
    assert r.status_code == 404

def test_subtopic_progress_requires_course_access(app_client, admin_session, tenant_a):
    """Without the entitlement gate a learner could write progress — and feed the
    activity gate — for a course they cannot open."""
    p, h = _login(app_client, admin_session, tenant_a)
    c = Course(tenant_id=tenant_a.id, slug="notmine", title="NM", discipline="networking", source_ref="x", version=1, status="published", listed=False)
    admin_session.add(c)
    admin_session.flush()
    admin_session.add(
        Chapter(
            tenant_id=tenant_a.id,
            course_id=c.id,
            number=1,
            title="One",
            part="I",
            body_html="<h2>Alpha</h2>",
            source_hash="sh2",
            order_index=1,
        )
    )
    admin_session.commit()

    app_client.get("/", headers=h)  # obtain a csrf cookie
    hh = {**h, "x-csrf-token": app_client.cookies.get("csrf_token") or ""}
    r = app_client.post("/courses/notmine/chapters/1/subtopics/alpha/complete", headers=hh)
    assert r.status_code == 403

@pytest.fixture
def chapter_owner_route(monkeypatch):
    """Adapter doubles only; installed Learning owns domain SQL/event canaries."""
    from types import SimpleNamespace
    from unittest.mock import Mock

    from app.services import settings_store
    from app.web import learn

    monkeypatch.setattr(settings_store, "get_all", Mock(return_value={}))
    tenant = SimpleNamespace(id=uuid.uuid4())
    person = SimpleNamespace(id=uuid.uuid4())
    course = SimpleNamespace(id=uuid.uuid4(), slug="owner-chapter")
    chapter = SimpleNamespace(id=uuid.uuid4(), body_html="<h2>Read me</h2>", number=1)
    request = object()
    db = Mock()
    db.scalars.side_effect = [
        Mock(first=Mock(return_value=course)),
        Mock(first=Mock(return_value=chapter)),
    ]
    gate = Mock()
    render = Mock(side_effect=lambda _request, _template, context: context)
    emit = Mock()
    monkeypatch.setattr(learn, "require_tenant", Mock(return_value=tenant))
    monkeypatch.setattr(learn, "require_course_open", gate)
    monkeypatch.setattr(learn.templates, "TemplateResponse", render)
    monkeypatch.setattr(learn.learning_events, "emit", emit)
    return SimpleNamespace(
        learn=learn,
        tenant=tenant,
        person=person,
        course=course,
        chapter=chapter,
        request=request,
        db=db,
        gate=gate,
        render=render,
        emit=emit,
    )

@pytest.mark.parametrize(
    "done,event_status", [(True, "recorded"), (True, "suppressed"), (True, "failed"), (False, None)]
)
def test_chapter_toggle_delegates_one_scoped_owner_command_and_renders_outcome(
    chapter_owner_route, monkeypatch, caplog, done, event_status
):
    from unittest.mock import Mock

    import dotmac_learning as learning

    case = chapter_owner_route
    event = learning.EventEmission(event_status) if event_status else None
    owner = Mock(return_value=learning.ChapterReadWrite(done, event))
    monkeypatch.setattr(learning, "toggle_chapter_read", owner)
    result = case.learn.chapter_complete(case.course.slug, 1, case.request, person=case.person, db=case.db)
    case.gate.assert_called_once_with(
        case.db, tenant_id=case.tenant.id, person_id=case.person.id, course_id=case.course.id
    )
    owner.assert_called_once_with(
        case.db,
        learning.ChapterReadingCommand(
            tenant_id=case.tenant.id,
            person_id=case.person.id,
            course_id=case.course.id,
            chapter_id=case.chapter.id,
        ),
        local_day_zone=None,
    )
    assert result["completed"] is done
    case.render.assert_called_once_with(
        case.request,
        "_mark_complete.html",
        {"request": case.request, "course": case.course, "chapter": case.chapter, "completed": done},
    )
    # The event consequence and persistence are delegated, never duplicated here.
    case.emit.assert_not_called()
    case.db.add.assert_not_called()
    case.db.delete.assert_not_called()
    case.db.flush.assert_not_called()
    case.db.execute.assert_not_called()
    assert case.db.scalars.call_count == 2
    warnings = [record.getMessage() for record in caplog.records if record.name == case.learn.reading_progress.__name__]
    assert warnings == (["learning-event emission failed (chapter_completed)"] if event_status == "failed" else [])

@pytest.mark.parametrize("route", ["chapter", "chapter_complete"])
def test_chapter_owner_access_denial_precedes_progress_or_render(chapter_owner_route, monkeypatch, route):
    from unittest.mock import Mock

    import dotmac_learning as learning
    from fastapi import HTTPException

    case = chapter_owner_route
    denied = HTTPException(status_code=403)
    case.gate.side_effect = denied
    reader, writer = Mock(), Mock()
    monkeypatch.setattr(learning, "completed_chapter_ids", reader)
    monkeypatch.setattr(learning, "toggle_chapter_read", writer)
    with pytest.raises(HTTPException) as caught:
        getattr(case.learn, route)(case.course.slug, 1, case.request, person=case.person, db=case.db)
    assert caught.value is denied
    assert case.db.scalars.call_count == 1
    reader.assert_not_called()
    writer.assert_not_called()
    case.emit.assert_not_called()
    case.render.assert_not_called()

@pytest.mark.parametrize("route", ["chapter", "chapter_complete"])
@pytest.mark.parametrize("missing", ["course", "chapter"])
def test_chapter_owner_missing_reference_keeps_404_before_progress(chapter_owner_route, monkeypatch, route, missing):
    from unittest.mock import Mock

    import dotmac_learning as learning
    from fastapi import HTTPException

    case = chapter_owner_route
    case.db.scalars.side_effect = (
        [Mock(first=Mock(return_value=None))]
        if missing == "course"
        else [Mock(first=Mock(return_value=case.course)), Mock(first=Mock(return_value=None))]
    )
    reader, writer = Mock(), Mock()
    monkeypatch.setattr(learning, "completed_chapter_ids", reader)
    monkeypatch.setattr(learning, "toggle_chapter_read", writer)
    with pytest.raises(HTTPException) as caught:
        getattr(case.learn, route)(case.course.slug, 1, case.request, person=case.person, db=case.db)
    assert caught.value.status_code == 404
    assert case.gate.call_count == (0 if missing == "course" else 1)
    reader.assert_not_called()
    writer.assert_not_called()
    case.emit.assert_not_called()
    case.render.assert_not_called()

@pytest.mark.parametrize("current_done", [False, True])
def test_chapter_get_keeps_sidebar_batch_and_later_current_flag_observations(
    chapter_owner_route, monkeypatch, current_done
):
    from types import SimpleNamespace
    from unittest.mock import Mock, call

    import dotmac_learning as learning

    case = chapter_owner_route
    other = SimpleNamespace(id=uuid.uuid4(), body_html="<h2>Other</h2>", number=2)
    chapter_ids = [case.chapter.id, other.id]
    case.db.scalars.side_effect = [
        Mock(first=Mock(return_value=case.course)),
        Mock(first=Mock(return_value=case.chapter)),
        Mock(all=Mock(return_value=[])),
        Mock(all=Mock(return_value=[case.chapter, other])),
        Mock(first=Mock(return_value=None)),
        Mock(first=Mock(return_value=other)),
    ]
    case.db.scalar.return_value = 2
    # A later READ COMMITTED observation may differ from the earlier sidebar.
    owner = Mock(side_effect=[{other.id}, {case.chapter.id} if current_done else set()])
    monkeypatch.setattr(learning, "completed_chapter_ids", owner)
    monkeypatch.setattr(case.learn.reading_progress, "completed_slugs_by_chapter", Mock(return_value={}))
    monkeypatch.setattr(case.learn, "best_scores_for", Mock(return_value={}))
    result = case.learn.chapter(case.course.slug, 1, case.request, person=case.person, db=case.db)
    assert owner.call_args_list == [
        call(case.db, tenant_id=case.tenant.id, person_id=case.person.id, chapter_ids=chapter_ids),
        call(case.db, tenant_id=case.tenant.id, person_id=case.person.id, chapter_ids=[case.chapter.id]),
    ]
    assert result["completed_chapter_ids"] == {other.id}
    assert result["completed_chapters"] == 1
    assert result["completed"] is current_done
    assert result["chapter"] is case.chapter and result["course"] is case.course
    assert result["chapters"] == [case.chapter, other]
    assert result["next_chapter"] is other and result["previous_chapter"] is None
    assert result["total_chapters"] == 2
    assert case.render.call_args.args[:2] == (case.request, "chapter.html")
    assert case.db.scalars.call_count == 6
    case.db.scalar.assert_called_once()
    case.db.execute.assert_not_called()
    assert [item.kwargs["kind"] for item in case.emit.call_args_list] == ["course_viewed", "chapter_viewed"]

def test_chapter_toggle_required_owner_failure_propagates_without_render(chapter_owner_route, monkeypatch):
    from unittest.mock import Mock

    import dotmac_learning as learning

    case = chapter_owner_route
    failure = RuntimeError("required persistence failure")
    owner = Mock(side_effect=failure)
    monkeypatch.setattr(learning, "toggle_chapter_read", owner)
    with pytest.raises(RuntimeError) as caught:
        case.learn.chapter_complete(case.course.slug, 1, case.request, person=case.person, db=case.db)
    assert caught.value is failure
    owner.assert_called_once()
    case.render.assert_not_called()
    case.emit.assert_not_called()
    case.db.commit.assert_not_called()
    case.db.rollback.assert_not_called()
