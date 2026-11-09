"""Hosted-only grant route attribution checks; no DB/audit persistence proof."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import dotmac_kernel.audit as kernel_audit
import dotmac_learning as learning
import pytest
from fastapi import HTTPException

from app.web import instructor


def _setup(monkeypatch, *, activity_missing=False, uncapped=False):
    session = object()
    tenant_id, instructor_id, target_id, activity_id = (uuid4() for _ in range(4))
    now = datetime(2026, 10, 17, tzinfo=UTC)
    activity = learning.ActivityFact(
        id=activity_id,
        tenant_id=tenant_id,
        course_id=uuid4(),
        title="Exam",
        chapter_number=None,
        type="mcq_test",
        bank_id=None,
        pass_threshold=0.6,
        max_attempts=None if uncapped else 2,
        assessment_mode="graded",
        grading="auto",
        weight=1.0,
        question_count=None,
        created_at=now,
        updated_at=now,
    )
    # None of these client-supplied actor hints may become audit authority.
    request = SimpleNamespace(
        state=SimpleNamespace(source_application="forged.host"),
        actor_id=str(target_id),
        granted_by=target_id,
    )
    person = SimpleNamespace(id=instructor_id)
    monkeypatch.setattr(instructor, "require_tenant", lambda request: SimpleNamespace(id=tenant_id))

    def get_activity(db, **kw):
        assert db is session and kw == {
            "tenant_id": tenant_id,
            "activity_id": activity_id,
        }
        return None if activity_missing else activity

    monkeypatch.setattr(instructor, "get_activity", get_activity)

    # The #188 scope checks run before the grant; both must receive the
    # authenticated person, never a client-supplied id.
    def course_ids(db, *, tenant_id: object, instructor_id: object):
        assert db is session and instructor_id == person.id
        return {activity.course_id}

    def person_ids(db, *, tenant_id: object, instructor_id: object, course_id: object):
        assert db is session and instructor_id == person.id and course_id == activity.course_id
        return frozenset({target_id})

    monkeypatch.setattr(instructor, "_assessment_course_ids", course_ids)
    monkeypatch.setattr(instructor, "_assessment_person_ids_for_course", person_ids)
    monkeypatch.setattr(kernel_audit, "host_application_or_none", lambda: "academy.installed.test")
    monkeypatch.setattr(instructor, "hx_redirect", lambda request, url: ("redirect", url))
    return session, tenant_id, person, target_id, activity_id, request


def test_authenticated_dependency_and_installed_host_own_grant_context(monkeypatch):
    session, tenant_id, person, target_id, activity_id, request = _setup(monkeypatch)
    now = datetime(2026, 10, 17, tzinfo=UTC)
    captured = []

    def owner(db, command, *, audit_context):
        assert db is session and isinstance(command, learning.GrantExtraAttemptCommand)
        assert command.person_id == target_id and command.granted_by == person.id
        assert command.reason == "Power cut" and command.extra_attempts == 2
        assert isinstance(audit_context, learning.AttemptAuditContext)
        assert audit_context.tenant_id == tenant_id and audit_context.actor_type == "user"
        assert audit_context.actor_id == str(person.id) and audit_context.actor_id != str(target_id)
        assert audit_context.source_application == "academy.installed.test"
        captured.append(audit_context)
        return learning.AttemptGrantFact(
            uuid4(),
            tenant_id,
            activity_id,
            target_id,
            2,
            command.reason,
            person.id,
            now,
            now,
        )

    monkeypatch.setattr(learning, "grant_extra_attempt", owner)
    assert instructor.grant_attempt(
        activity_id,
        request,
        person=person,
        person_id=target_id,
        reason="Power cut",
        extra_attempts=2,
        db=session,
    ) == ("redirect", "/instructor/results")
    assert len(captured) == 1


def test_uninstalled_host_refuses_before_grant_and_is_not_a_reason_error(monkeypatch):
    session, tenant_id, person, target_id, activity_id, request = _setup(monkeypatch)
    monkeypatch.setattr(kernel_audit, "host_application_or_none", lambda: None)
    monkeypatch.setattr(
        learning,
        "grant_extra_attempt",
        lambda *a, **kw: pytest.fail("grant ran without host attribution"),
    )
    with pytest.raises(kernel_audit.UnattributedAuditEventError):
        instructor.grant_attempt(
            activity_id,
            request,
            person=person,
            person_id=target_id,
            reason="Power cut",
            extra_attempts=1,
            db=session,
        )


@pytest.mark.parametrize(("missing", "uncapped", "status"), [(True, False, 404), (False, True, 400)])
def test_existing_http_guards_precede_attribution_and_writer(monkeypatch, missing, uncapped, status):
    session, tenant_id, person, target_id, activity_id, request = _setup(
        monkeypatch, activity_missing=missing, uncapped=uncapped
    )
    monkeypatch.setattr(
        instructor,
        "resolve_event_attribution",
        lambda *a: pytest.fail("host resolved before existing guard"),
    )
    monkeypatch.setattr(
        learning,
        "grant_extra_attempt",
        lambda *a, **kw: pytest.fail("grant ignored existing guard"),
    )
    with pytest.raises(HTTPException) as exc:
        instructor.grant_attempt(
            activity_id,
            request,
            person=person,
            person_id=target_id,
            reason="Power cut",
            extra_attempts=1,
            db=session,
        )
    assert exc.value.status_code == status
    if uncapped:
        assert exc.value.detail == "This activity has no attempt limit."


def test_required_grant_reason_error_keeps_existing_http_mapping(monkeypatch):
    session, tenant_id, person, target_id, activity_id, request = _setup(monkeypatch)

    def owner(*args, **kw):
        raise ValueError("a grant must record why it was given")

    monkeypatch.setattr(learning, "grant_extra_attempt", owner)
    with pytest.raises(HTTPException) as exc:
        instructor.grant_attempt(
            activity_id,
            request,
            person=person,
            person_id=target_id,
            reason="   ",
            extra_attempts=1,
            db=session,
        )
    assert exc.value.status_code == 400 and exc.value.detail == "a grant must record why it was given"
