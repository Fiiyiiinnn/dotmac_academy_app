"""Policy composition boundaries; fake owners prove inputs, never database parity."""

from types import SimpleNamespace
from uuid import uuid4

import dotmac_learning as learning
import pytest

from app.services import attempt_policy, authoring, bank_loader, grading, settings_store


def test_authoring_uses_configured_listed_and_preserves_explicit_choice(monkeypatch):
    session, tenant = object(), uuid4()
    commands = []

    def effective(db, *, require_store):
        assert db is session and require_store is True
        return {"learning_default_course_listed": True}

    def create(db, command):
        assert db is session
        commands.append(command)
        return SimpleNamespace(id=uuid4())

    # The remaining course-creation defaults are composed policy read on the same session
    # (product defaults); stub that reader and prove its values reach the owner command.
    def creation_defaults(db):
        assert db is session
        return SimpleNamespace(
            completion_fraction=1.0,
            certificate_auto_issue=True,
            certificate_validity_days=None,
            prerequisite_default_composition="all",
        )

    monkeypatch.setattr(settings_store, "effective", effective)
    monkeypatch.setattr(authoring, "creation_defaults", creation_defaults)
    monkeypatch.setattr(learning, "create_draft_course", create)
    authoring.create_course(session, tenant_id=tenant, slug="x", title="X", discipline="x")
    authoring.create_course(session, tenant_id=tenant, slug="y", title="Y", discipline="x", listed=False)
    assert [command.listed for command in commands] == [True, False]
    assert all(
        (c.completion_fraction, c.certificate_auto_issue, c.certificate_validity_days, c.prerequisite_composition)
        == (1.0, True, None, "all")
        for c in commands
    )


def test_bank_adapter_passes_configured_weight_without_mutating_raw_doc(monkeypatch):
    # Honest seam after the retired ``_load_bank`` re-export was removed:
    # the adapter converts the raw doc and forwards a Learning import
    # command to ``learning.import_bank_document_as_draft``.
    assert not hasattr(bank_loader, "_load_bank")
    session = object()
    raw_question = {
        "id": "q1",
        "stem": "S?",
        "type": "single",
        "rubric_category": "recall",
        "options": [True, None],
    }
    doc = learning.BankDoc("source", None, "final", 1, [raw_question])
    calls = []
    bank_id = uuid4()
    bank_fact = SimpleNamespace(id=bank_id)

    def effective(db, *, require_store):
        assert db is session and require_store is True
        return {"learning_missing_question_weight": 9}

    def owner(db, command, *, audit_context):
        assert db is session
        calls.append(command)
        return SimpleNamespace(bank_id=bank_id)

    monkeypatch.setattr(settings_store, "effective", effective)
    monkeypatch.setattr(learning, "import_bank_document_as_draft", owner)
    monkeypatch.setattr(learning, "get_bank_fact", lambda db, *, tenant_id, bank_id: bank_fact)
    result = bank_loader.load_bank(session, tenant_id=uuid4(), course_id=uuid4(), doc=doc)
    assert result is bank_fact
    assert calls[0].missing_question_weight == 9
    assert type(calls[0].doc) is learning.BankDocument
    assert calls[0].doc.questions[0].weight == 9
    assert doc.questions == [raw_question]
    assert "weight" not in raw_question


def test_absent_required_setting_refuses_before_owner(monkeypatch):
    monkeypatch.setattr(settings_store, "effective", lambda db, *, require_store: {})
    monkeypatch.setattr(
        learning, "import_bank_document_as_draft", lambda *args, **kwargs: pytest.fail("owner called")
    )
    with pytest.raises(KeyError, match="learning_missing_question_weight"):
        bank_loader.load_bank(
            object(), tenant_id=uuid4(), course_id=uuid4(), doc=learning.BankDoc("x", None, "final", 1, [])
        )


def test_grant_omission_composes_setting_and_explicit_count_wins(monkeypatch):
    calls = []
    monkeypatch.setattr(
        settings_store,
        "effective",
        lambda db, *, require_store: {
            "learning_default_extra_attempts": 4,
        },
    )

    def owner(db, command, *, audit_context):
        calls.append(command.extra_attempts)
        return SimpleNamespace(id=uuid4())

    monkeypatch.setattr(learning, "grant_extra_attempt", owner)
    values = {
        "tenant_id": uuid4(),
        "person_id": uuid4(),
        "activity_id": uuid4(),
        "reason": "outage",
        "audit_context": object(),
    }
    attempt_policy.grant_extra_attempt(object(), **values)
    attempt_policy.grant_extra_attempt(object(), **values, extra_attempts=2)
    assert calls == [4, 2]


def test_grader_item_ids_correspond_exactly_to_explicit_question_weights():
    questions = [
        {"ext_id": "a", "correct": ["A"], "weight": 2.5},
        {"ext_id": "b", "correct": ["B"], "weight": 7},
    ]
    result = grading.grade_submission({"a": ["A"], "b": ["B"]}, questions, 0, missing_question_weight=11)
    weight_of = {q["ext_id"]: float(q["weight"]) for q in questions}
    assert [item["id"] for item in result.per_item] == list(weight_of)
    assert [weight_of[item["id"]] for item in result.per_item] == [2.5, 7.0]
    assert result.max_score == result.score == 9.5
