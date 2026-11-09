"""Hosted-only access/pacing adapters; composed submit test needs reviewed routes."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import dotmac_learning as learning
import pytest
from fastapi import HTTPException
from starlette.datastructures import FormData

from app.services import entitlements, pacing
from app.web import learn


@pytest.mark.parametrize(
    "name",
    [
        "visible_course_ids",
        "course_access_states",
        "accessible_course_ids",
        "person_can_access_course",
        "open_course_ids",
        "unmet_prerequisites",
    ],
)
@pytest.mark.parametrize("now", [None, datetime(2026, 10, 17, 23, 30, tzinfo=UTC)])
def test_access_reads_forward_scope_and_return_owner_collections(monkeypatch, name, now):
    session = object()
    tenant_id, person_id, course_id, required_id = (uuid4() for _ in range(4))
    state = learning.CourseAccessState(course_id, True, 'Complete "First" to unlock this course.', required_id)
    values = {
        "visible_course_ids": {course_id, required_id},
        "course_access_states": {
            course_id: state,
            required_id: learning.CourseAccessState(required_id, False),
        },
        "accessible_course_ids": set(),
        "person_can_access_course": False,
        "open_course_ids": {course_id},
        "unmet_prerequisites": [required_id, required_id],
    }
    args = {"tenant_id": tenant_id, "person_id": person_id}
    if name in {"person_can_access_course", "unmet_prerequisites"}:
        args["course_id"] = course_id
    if name == "open_course_ids":
        args["now"] = now
    calls = []

    def owner(db, **kw):
        assert db is session and kw == args
        calls.append(kw)
        return values[name]

    monkeypatch.setattr(learning, name, owner)
    result = getattr(entitlements, name)(session, **args)
    assert result is values[name] and len(calls) == 1
    assert entitlements.CourseAccessState is learning.CourseAccessState
    if name == "course_access_states":
        assert list(result) == [course_id, required_id]
        assert result[course_id] is state and result[required_id].locked_reason is None
    elif name == "unmet_prerequisites":
        assert result == [required_id, required_id]  # no adapter deduplication


@pytest.mark.parametrize(
    "fact",
    [
        learning.PacingFact(True, True, False),
        learning.PacingFact(False, False, True),
        learning.PacingFact(True, False, True),
        learning.PacingFact(True, True, True),
    ],
)
@pytest.mark.parametrize("now", [None, datetime(2026, 10, 17, tzinfo=UTC)])
def test_pacing_fact_maps_only_the_three_existing_fields(monkeypatch, fact, now):
    session = object()
    args = dict(
        zip(
            ("tenant_id", "person_id", "course_id", "activity_id"),
            (uuid4() for _ in range(4)),
            strict=True,
        )
    )
    args["now"] = now
    calls = []

    def owner(db, **kw):
        assert db is session and kw == args
        calls.append(kw)
        return fact

    monkeypatch.setattr(learning, "activity_pacing", owner)
    assert pacing.activity_pacing(session, **args) == {
        "readable": fact.readable,
        "submittable": fact.submittable,
        "has_override": fact.has_override,
    }
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("module", "name", "detail"),
    [
        (entitlements, "require_course_access", "Forbidden"),
        (entitlements, "require_course_open", "Forbidden"),
        (
            entitlements,
            "require_course_open",
            "Complete the previous course to unlock this course.",
        ),
        (pacing, "require_activity_readable", "Activity not yet released"),
        (
            pacing,
            "require_activity_submittable",
            "Activity is not open for submission (not released or past due)",
        ),
    ],
)
@pytest.mark.parametrize("outcome", ["allow", "deny", "required-fault"])
def test_guards_map_only_domain_denials_to_existing_http(monkeypatch, module, name, detail, outcome):
    session = object()
    args = {"tenant_id": uuid4(), "person_id": uuid4(), "course_id": uuid4()}
    if module is pacing:
        args["activity_id"] = uuid4()
    if name != "require_course_access":
        args["now"] = None
    failure = RuntimeError("required owner read failed")
    denial = learning.PacingForbidden(detail) if module is pacing else learning.CourseAccessDenied(detail)
    calls = []

    def owner(db, **kw):
        assert db is session and kw == args
        calls.append(kw)
        if outcome == "deny":
            raise denial
        if outcome == "required-fault":
            raise failure

    monkeypatch.setattr(learning, name, owner)
    if outcome == "allow":
        assert getattr(module, name)(session, **args) is None
    elif outcome == "deny":
        with pytest.raises(HTTPException) as raised:
            getattr(module, name)(session, **args)
        assert raised.value.status_code == 403 and raised.value.detail == detail
        assert raised.value.headers is None
        assert raised.value.__cause__ is denial
    else:
        with pytest.raises(RuntimeError) as raised:
            getattr(module, name)(session, **args)
        assert raised.value is failure
    assert len(calls) == 1


@pytest.mark.parametrize("blocked", ["course", "pacing", None])
def test_reviewed_submit_caller_preserves_guard_order_before_form_and_manual_path(monkeypatch, blocked):
    """Compose real product guards with the independently reviewed submit route."""
    session = object()
    tenant_id, person_id, course_id, activity_id = (uuid4() for _ in range(4))
    now = datetime(2026, 10, 17, tzinfo=UTC)
    activity = learning.ActivityFact(
        id=activity_id,
        tenant_id=tenant_id,
        course_id=course_id,
        title="Review",
        chapter_number=None,
        type="mcq_test",
        bank_id=None,
        pass_threshold=0,
        max_attempts=None,
        assessment_mode="graded",
        grading="manual",
        weight=1,
        question_count=None,
        created_at=now,
        updated_at=now,
    )
    calls = []

    async def form():
        calls.append("form")
        assert blocked is None
        return FormData()

    request = SimpleNamespace(form=form)
    person = SimpleNamespace(id=person_id)
    monkeypatch.setattr(learn, "require_tenant", lambda request: SimpleNamespace(id=tenant_id))
    monkeypatch.setattr(learn, "get_activity", lambda db, **kw: activity)

    def course_guard(db, **kw):
        assert db is session and kw == {
            "tenant_id": tenant_id,
            "person_id": person_id,
            "course_id": course_id,
            "now": None,
        }
        calls.append("course")
        if blocked == "course":
            raise learning.CourseAccessDenied()

    def pacing_guard(db, **kw):
        assert db is session and kw == {
            "tenant_id": tenant_id,
            "person_id": person_id,
            "course_id": course_id,
            "activity_id": activity_id,
            "now": None,
        }
        calls.append("pacing")
        if blocked == "pacing":
            raise learning.PacingForbidden("Activity is not open for submission (not released or past due)")

    def questions(db, **kw):
        assert db is session
        calls.append("questions")
        return []

    def submit(db, **kw):
        assert db is session and kw["activity"] is activity and kw["answers"] == {}
        calls.append("manual-submit")
        return None

    monkeypatch.setattr(learning, "require_course_open", course_guard)
    monkeypatch.setattr(learning, "require_activity_submittable", pacing_guard)
    # Bind the actual adapter functions, not surrogate permission predicates.
    monkeypatch.setattr(learn, "require_course_open", entitlements.require_course_open)
    monkeypatch.setattr(learn, "require_activity_submittable", pacing.require_activity_submittable)
    monkeypatch.setattr(learn, "questions_for", questions)
    monkeypatch.setattr(learn, "submit_activity", submit)
    if blocked:
        with pytest.raises(HTTPException) as raised:
            asyncio.run(learn.submit(activity_id, request, person=person, db=session))
        assert raised.value.status_code == 403 and raised.value.headers is None
        assert calls == (["course"] if blocked == "course" else ["course", "pacing"])
    else:
        response = asyncio.run(learn.submit(activity_id, request, person=person, db=session))
        assert response.status_code == 200 and b"your instructor" in response.body
        assert calls == ["course", "pacing", "form", "questions", "manual-submit"]
