"""Public announcement adapter boundary cases; runtime qualification is hosted."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import dotmac_learning as learning
import pytest
from pydantic import ValidationError

from app.services import announcements


def _fact(tenant_id, author_id, cohort_id=None):
    now = datetime(2026, 10, 17, tzinfo=UTC)
    return learning.AnnouncementFact(
        id=uuid4(),
        tenant_id=tenant_id,
        cohort_id=cohort_id,
        author_person_id=author_id,
        title="Notice",
        body_md="**body**",
        body_html="<p><strong>body</strong></p>",
        created_at=now,
        updated_at=now,
    )


@pytest.mark.parametrize("targeted", [False, True])
def test_create_renders_once_then_delegates_real_command_and_trusted_context(monkeypatch, targeted):
    session = object()
    tenant_id, author_id = uuid4(), uuid4()
    cohort_id = uuid4() if targeted else None
    fact = _fact(tenant_id, author_id, cohort_id)
    events = []
    command_type = learning.CreateAnnouncementCommand

    def render(body):
        assert body == fact.body_md
        events.append("render")
        return fact.body_html

    def construct(**fields):
        events.append("command")
        return command_type(**fields)

    def source(value):
        assert value is None
        events.append("host")
        return "academy.test"

    def owner(db, command, *, audit_context, fanout):
        events.append("owner")
        assert db is session
        assert isinstance(command, command_type)
        assert command.model_dump() == {
            "tenant_id": tenant_id,
            "author_person_id": author_id,
            "title": fact.title,
            "body_md": fact.body_md,
            "body_html": fact.body_html,
            "cohort_id": cohort_id,
        }
        assert isinstance(audit_context, learning.AttemptAuditContext)
        assert audit_context.tenant_id == tenant_id
        assert audit_context.actor_type == "user"
        assert audit_context.actor_id == str(author_id)
        assert audit_context.actor_label is None
        assert audit_context.source_application == "academy.test"
        assert fanout is announcements._notify
        return fact

    monkeypatch.setattr(announcements, "render_markdown", render)
    monkeypatch.setattr(learning, "CreateAnnouncementCommand", construct)
    monkeypatch.setattr(announcements, "resolve_event_attribution", source)
    monkeypatch.setattr(learning, "create_announcement", owner)
    kwargs = {"cohort_id": cohort_id} if targeted else {}
    assert (
        announcements.create(
            session,
            tenant_id=tenant_id,
            author_person_id=author_id,
            title=fact.title,
            body_md=fact.body_md,
            **kwargs,
        )
        is fact
    )
    assert events == ["render", "command", "host", "owner"]


def test_notification_callback_preserves_session_payload_order_and_duplicates(monkeypatch):
    session = object()
    fact = _fact(uuid4(), uuid4())
    first, second = uuid4(), uuid4()
    recipients = (second, first, second)
    calls = []

    def notify(db, **kwargs):
        assert db is session
        calls.append(kwargs)

    monkeypatch.setattr(announcements, "notify_many", notify)
    announcements._notify(session, fact, recipients)
    assert calls == [
        {
            "tenant_id": fact.tenant_id,
            "person_ids": list(recipients),
            "kind": "announcement",
            "title": fact.title,
            "body": "",
            "link": "/announcements",
        }
    ]
    assert type(calls[0]["person_ids"]) is list


def test_callback_failure_is_left_to_module_savepoint_owner(monkeypatch):
    failure = RuntimeError("notification insert")

    def notify(*args, **kwargs):
        raise failure

    monkeypatch.setattr(announcements, "notify_many", notify)
    with pytest.raises(RuntimeError) as caught:
        announcements._notify(object(), _fact(uuid4(), uuid4()), (uuid4(),))
    assert caught.value is failure


@pytest.mark.parametrize("stage", ["render", "host", "owner"])
def test_required_failure_propagates_without_fallback_or_second_writer(monkeypatch, stage):
    failure = RuntimeError(stage)
    calls = []

    def step(name, result):
        def invoke(*args, **kwargs):
            calls.append(name)
            if stage == name:
                raise failure
            return result

        return invoke

    monkeypatch.setattr(announcements, "render_markdown", step("render", "<p>body</p>"))
    monkeypatch.setattr(announcements, "resolve_event_attribution", step("host", "academy.test"))
    monkeypatch.setattr(learning, "create_announcement", step("owner", None))
    with pytest.raises(RuntimeError) as caught:
        announcements.create(object(), tenant_id=uuid4(), author_person_id=uuid4(), title="Notice", body_md="body")
    assert caught.value is failure
    assert calls == ["render", "host", "owner"][: ["render", "host", "owner"].index(stage) + 1]


def test_command_validation_occurs_after_render_before_host_or_owner(monkeypatch):
    calls = []
    monkeypatch.setattr(announcements, "render_markdown", lambda body: calls.append(body) or "<p>body</p>")

    def unexpected(*args, **kwargs):
        pytest.fail("invalid command reached attribution or persistence")

    monkeypatch.setattr(announcements, "resolve_event_attribution", unexpected)
    monkeypatch.setattr(learning, "create_announcement", unexpected)
    with pytest.raises(ValidationError):
        announcements.create(object(), tenant_id="invalid", author_person_id=uuid4(), title="Notice", body_md="body")
    assert calls == ["body"]


@pytest.mark.parametrize(
    "reader, public, default",
    [
        ("for_person", "announcements_for_person", 20),
        ("list_for_tenant", "list_announcements", 50),
    ],
)
@pytest.mark.parametrize("limit", [None, 0, 3, -1])
@pytest.mark.parametrize("empty", [False, True])
def test_readers_preserve_list_container_fact_identity_order_and_limits(
    monkeypatch, reader, public, default, limit, empty
):
    session = object()
    tenant_id, person_id = uuid4(), uuid4()
    facts = () if empty else (_fact(tenant_id, person_id), _fact(tenant_id, person_id))
    kwargs = {"tenant_id": tenant_id}
    if reader == "for_person":
        kwargs["person_id"] = person_id
    expected = dict(kwargs, limit=default if limit is None else limit)

    def owner(db, **fields):
        assert db is session and fields == expected
        return facts

    monkeypatch.setattr(learning, public, owner)
    # An omitted limit is the strict settings-store policy value (product defaults 20 / 50); stub only
    # that reader so the session double still rejects any other database use.
    policy = {"learning_person_announcements_limit": 20, "learning_tenant_announcements_limit": 50}

    def effective(db, *, require_store):
        assert db is session and require_store is True
        return policy

    monkeypatch.setattr(announcements, "effective", effective)
    if limit is not None:
        kwargs["limit"] = limit
    result = getattr(announcements, reader)(session, **kwargs)
    assert type(result) is list
    assert len(result) == len(facts)
    assert all(actual is fact for actual, fact in zip(result, facts, strict=True))


def test_delete_delegates_real_scoped_command_without_audit_attribution(monkeypatch):
    session = object()
    tenant_id, announcement_id = uuid4(), uuid4()
    calls = []

    def owner(db, command):
        assert db is session
        assert isinstance(command, learning.DeleteAnnouncementCommand)
        assert command.model_dump() == {"tenant_id": tenant_id, "announcement_id": announcement_id}
        calls.append(command)

    def unexpected(*args, **kwargs):
        pytest.fail("delete must not resolve audit attribution")

    monkeypatch.setattr(learning, "delete_announcement", owner)
    monkeypatch.setattr(announcements, "resolve_event_attribution", unexpected)
    assert announcements.delete(session, tenant_id=tenant_id, announcement_id=announcement_id) is None
    assert len(calls) == 1


def test_delete_required_failure_propagates(monkeypatch):
    failure = RuntimeError("delete flush")

    def owner(db, command):
        raise failure

    monkeypatch.setattr(learning, "delete_announcement", owner)
    with pytest.raises(RuntimeError) as caught:
        announcements.delete(object(), tenant_id=uuid4(), announcement_id=uuid4())
    assert caught.value is failure
