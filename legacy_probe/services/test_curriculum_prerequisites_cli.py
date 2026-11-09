"""CLI prerequisite boundary canaries; runtime execution belongs to hosted CI."""

from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import dotmac_learning as learning
import pytest
import yaml
from pydantic import ValidationError

from app import cli
from app.services.learning_policy import LearningCreationDefaults


def _defaults(**overrides):
    values = dict(
        prerequisite_default_kind="course_completed",
        prerequisite_default_composition="all",
        track_default_gating_mode="previous",
        completion_fraction=1.0,
        certificate_auto_issue=True,
        certificate_validity_days=None,
        activity_scoring_policy="best",
        attempt_cooldown_seconds=0,
        session_promotes_delivery_mode=True,
        pacing_default_open=True,
    )
    values.update(overrides)
    return LearningCreationDefaults(**values)


def _boundary(monkeypatch, document, *, dry_run=False):
    events = []
    tenant = SimpleNamespace(id=uuid4())
    db = SimpleNamespace(commit=lambda: events.append("commit"), rollback=lambda: events.append("rollback"))

    def read_text():
        events.append("read")
        return "source YAML"

    def parse(source):
        assert source == "source YAML"
        events.append("parse")
        return {"curriculum": document}

    @contextmanager
    def session(slug):
        assert slug == "actual-tenant"
        events.append("session-enter")
        try:
            yield db, tenant
        finally:
            events.append("session-exit")

    monkeypatch.setattr(yaml, "safe_load", parse)
    monkeypatch.setattr(cli, "_tenant_session", session)
    monkeypatch.setattr(
        "app.services.learning_policy.creation_defaults", lambda session: _defaults()
    )
    args = SimpleNamespace(file=SimpleNamespace(read_text=read_text), tenant_slug="actual-tenant", dry_run=dry_run)
    return args, db, tenant, events


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("discipline", ["networking", None])
def test_load_curriculum_delegates_same_session_tenant_and_counts(monkeypatch, capsys, dry_run, discipline):
    document = {
        "discipline": discipline,
        "prerequisites": [
            {"course": " raw course ", "requires": ["b", "a", "b"], "ignored": "extra"},
            {"course": " raw course ", "requires": ["c"]},
            {"course": "not-imported-but-no-requirements", "requires": []},
        ],
        "ignored": "top-level extra",
    }
    args, db, tenant, events = _boundary(monkeypatch, document, dry_run=dry_run)
    seen = []

    def owner(session, command):
        seen.append(command)
        assert session is db
        assert events == ["read", "parse", "session-enter"]
        events.append("owner")
        return learning.PrerequisiteReconciliationResult(added=3, removed=2, declared=4)

    monkeypatch.setattr(learning, "reconcile_prerequisites", owner)
    cli._load_curriculum(args)
    command = seen[0]
    assert command.tenant_id == tenant.id
    assert command.discipline == discipline
    assert [(item.course, item.requires) for item in command.declarations] == [
        (" raw course ", ("b", "a", "b")),
        (" raw course ", ("c",)),
        ("not-imported-but-no-requirements", ()),
    ]
    # Historical defaults keep the legacy path: no typed conditions are
    # materialized and authored composition is never restated.
    assert all(item.conditions == () and item.composition is None for item in command.declarations)
    assert events == ["read", "parse", "session-enter", "owner", "rollback" if dry_run else "commit", "session-exit"]
    expected = "[dry-run] would add 3, remove 2\n" if dry_run else "prerequisites: 3 added, 2 removed, 4 declared\n"
    assert capsys.readouterr().out == expected


@pytest.mark.parametrize("requires", [None, False, 0, "", [], (), {}])
def test_falsey_requires_normalizes_to_empty_without_local_slug_lookup(monkeypatch, requires):
    args, db, tenant, events = _boundary(
        monkeypatch, {"prerequisites": [{"course": "missing-from-db", "requires": requires}]}
    )
    seen = []

    def owner(session, command):
        assert session is db
        seen.append(command)
        return learning.PrerequisiteReconciliationResult(0, 0, 0)

    monkeypatch.setattr(learning, "reconcile_prerequisites", owner)
    cli._load_curriculum(args)
    assert seen[0].declarations[0].course == "missing-from-db"
    assert seen[0].declarations[0].requires == ()
    assert seen[0].discipline is None
    assert events[-2:] == ["commit", "session-exit"]


@pytest.mark.parametrize("document", [{}, {"prerequisites": None}, {"prerequisites": False}, {"prerequisites": []}])
def test_falsey_declarations_produce_empty_command(monkeypatch, document):
    args, db, tenant, events = _boundary(monkeypatch, document)
    seen = []

    def owner(session, command):
        seen.append(command)
        return learning.PrerequisiteReconciliationResult(0, 0, 0)

    monkeypatch.setattr(learning, "reconcile_prerequisites", owner)
    cli._load_curriculum(args)
    assert seen[0].declarations == ()
    assert seen[0].tenant_id == tenant.id


@pytest.mark.parametrize(
    "message",
    [
        "course cannot require itself",
        "These courses are declared in the file but not imported: a, z",
        "prerequisite cycle detected; refusing to write",
    ],
)
def test_graph_domain_errors_keep_exact_message_and_context_owns_failure(monkeypatch, capsys, message):
    args, db, tenant, events = _boundary(monkeypatch, {"prerequisites": [{"course": "a", "requires": ["b"]}]})
    error = learning.PrerequisiteReconciliationError(message)

    def owner(session, command):
        assert session is db
        events.append("owner")
        raise error

    monkeypatch.setattr(learning, "reconcile_prerequisites", owner)
    with pytest.raises(SystemExit) as caught:
        cli._load_curriculum(args)
    assert caught.value.code == message
    assert caught.value.__cause__ is error
    assert events == ["read", "parse", "session-enter", "owner", "session-exit"]
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("error", [RuntimeError("database unavailable"), ValueError("unrelated failure")])
def test_unrelated_owner_failure_propagates_without_cli_transaction_actions(monkeypatch, error):
    args, db, tenant, events = _boundary(monkeypatch, {})

    def owner(session, command):
        raise error

    monkeypatch.setattr(learning, "reconcile_prerequisites", owner)
    with pytest.raises(type(error)) as caught:
        cli._load_curriculum(args)
    assert caught.value is error
    assert events == ["read", "parse", "session-enter", "session-exit"]


@pytest.mark.parametrize("stage", ["read", "parse", "missing-curriculum"])
def test_filesystem_and_yaml_fail_before_tenant_session(monkeypatch, stage):
    failure = OSError("unreadable source") if stage == "read" else yaml.YAMLError("invalid YAML")
    events = []

    def read_text():
        events.append("read")
        if stage == "read":
            raise failure
        return "source"

    def parse(source):
        events.append("parse")
        if stage == "parse":
            raise failure
        return {}

    def unexpected_session(slug):
        pytest.fail("tenant session opened before filesystem/YAML validation")

    monkeypatch.setattr(yaml, "safe_load", parse)
    monkeypatch.setattr(cli, "_tenant_session", unexpected_session)
    args = SimpleNamespace(file=SimpleNamespace(read_text=read_text), tenant_slug="actual-tenant", dry_run=False)
    expected = KeyError if stage == "missing-curriculum" else type(failure)
    with pytest.raises(expected) as caught:
        cli._load_curriculum(args)
    if stage != "missing-curriculum":
        assert caught.value is failure
    assert events == (["read"] if stage == "read" else ["read", "parse"])


def test_missing_course_key_preserves_keyerror_without_owner_or_cli_transaction_action(monkeypatch):
    args, db, tenant, events = _boundary(monkeypatch, {"prerequisites": [{"requires": []}]})
    monkeypatch.setattr(
        learning, "reconcile_prerequisites", lambda *args: pytest.fail("owner reached with missing course key")
    )
    with pytest.raises(KeyError) as caught:
        cli._load_curriculum(args)
    assert caught.value.args == ("course",)
    assert events == ["read", "parse", "session-enter", "session-exit"]


@pytest.mark.parametrize(
    "entry", [{"course": 123, "requires": []}, {"course": "a", "requires": "b"}, {"course": "a", "requires": [False]}]
)
def test_malformed_untyped_entries_refuse_at_typed_boundary(monkeypatch, entry):
    args, db, tenant, events = _boundary(monkeypatch, {"prerequisites": [entry]})
    monkeypatch.setattr(
        learning, "reconcile_prerequisites", lambda *args: pytest.fail("malformed command reached owner")
    )
    with pytest.raises(ValidationError):
        cli._load_curriculum(args)
    assert events == ["read", "parse", "session-enter", "session-exit"]


def test_explicit_conditions_use_creation_default_composition_when_omitted(monkeypatch):
    document = {
        "prerequisites": [
            {
                "course": "advanced",
                "conditions": [{"kind": "certificate_held", "requires": "foundation"}],
            }
        ]
    }
    args, *_ = _boundary(monkeypatch, document)
    seen = []

    def owner(session, command):
        seen.append(command)
        return learning.PrerequisiteReconciliationResult(1, 0, 1)

    monkeypatch.setattr(learning, "reconcile_prerequisites", owner)
    cli._load_curriculum(args)
    (declaration,) = seen[0].declarations
    assert declaration.composition == "all"
    assert [(c.kind, c.requires, c.group) for c in declaration.conditions] == [
        ("certificate_held", "foundation", "g0")
    ]


def test_non_default_param_free_kind_materializes_legacy_requires(monkeypatch):
    args, *_ = _boundary(
        monkeypatch, {"prerequisites": [{"course": "advanced", "requires": ["foundation"]}]}
    )
    monkeypatch.setattr(
        "app.services.learning_policy.creation_defaults",
        lambda session: _defaults(prerequisite_default_kind="certificate_held"),
    )
    seen = []

    def owner(session, command):
        seen.append(command)
        return learning.PrerequisiteReconciliationResult(1, 0, 1)

    monkeypatch.setattr(learning, "reconcile_prerequisites", owner)
    cli._load_curriculum(args)
    (declaration,) = seen[0].declarations
    assert declaration.requires == ()
    assert declaration.composition == "all"
    assert [(c.kind, c.requires) for c in declaration.conditions] == [("certificate_held", "foundation")]


def test_param_kind_default_refuses_legacy_requires_before_owner(monkeypatch):
    args, *_ = _boundary(
        monkeypatch, {"prerequisites": [{"course": "advanced", "requires": ["foundation"]}]}
    )
    monkeypatch.setattr(
        "app.services.learning_policy.creation_defaults",
        lambda session: _defaults(prerequisite_default_kind="course_grade_at_least"),
    )
    monkeypatch.setattr(
        learning, "reconcile_prerequisites", lambda *a: pytest.fail("owner reached")
    )
    with pytest.raises(SystemExit, match="needs typed conditions"):
        cli._load_curriculum(args)
