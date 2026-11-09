"""Hosted adapter characterization using the actual public decoded input API."""

from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import dotmac_learning as learning
import dotmac_learning_bank_rules as bank_rules
import pytest

from app.services import bank_lint, bank_loader


def test_rule_exports_are_the_actual_public_owners():
    assert bank_lint.BankDoc is bank_rules.BankDoc
    assert bank_lint.BankDoc is learning.BankDoc
    assert bank_lint.lint_bank is bank_rules.lint_bank
    assert bank_lint.balance_targets is bank_rules.balance_targets
    assert bank_lint.propose_balance is bank_rules.propose_balance
    assert bank_loader.BankDoc is learning.BankDoc
    assert bank_loader.lint_bank is bank_rules.lint_bank
    assert bank_loader.parse_bank is bank_lint.parse_bank


def test_parser_keeps_decoded_values_and_legacy_container_rules(tmp_path):
    path = tmp_path / "bank.yaml"
    path.write_text(
        "bank:\n  course: source\n  kind: chapter\n  version: '2'\n"
        "  questions:\n    - id: one\n      weight: 1.9\n"
        "      category: 2026-10-17\n      options: [true, null]\n"
        "  policy: {pool: true, max_attempts: null}\n",
        encoding="utf-8",
    )
    doc = bank_lint.parse_bank(path)
    assert type(doc) is learning.BankDoc
    assert doc.version == 2
    assert doc.chapter is None
    assert type(doc.questions) is list
    assert doc.questions == [
        {
            "id": "one",
            "weight": 1.9,
            "category": date(2026, 10, 17),
            "options": [True, None],
        }
    ]
    assert doc.policy == {"pool": True, "max_attempts": None}
    doc.questions.append({"id": "mutable"})
    assert len(doc.questions) == 2


@pytest.mark.parametrize("policy", ["", "  policy: null\n", "  policy: {}\n"])
def test_parser_absent_null_empty_policy_stays_empty(tmp_path, policy):
    path = tmp_path / "bank.yaml"
    path.write_text(
        "bank:\n  course: source\n  kind: final\n  questions: []\n" + policy,
        encoding="utf-8",
    )
    assert bank_lint.parse_bank(path).policy == {}


def test_parser_bad_version_fails_before_pure_lint(tmp_path, monkeypatch):
    path = tmp_path / "bank.yaml"
    path.write_text(
        "bank:\n  course: source\n  kind: final\n  version: bad\n  questions: []\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bank_lint, "lint_bank", lambda doc: pytest.fail("lint before decode"))
    with pytest.raises(ValueError):
        bank_lint.parse_bank(path)


def test_lint_diagnostic_order_uses_raw_public_doc():
    doc = learning.BankDoc(
        course="source",
        chapter=1,
        kind="chapter",
        version=1,
        questions=[{"id": "one", "rubric_category": "invalid"}],
        policy={
            "unknown": 1,
            "pool": 2,
            "max_attempts": 0,
            "pass_threshold": True,
            "mode": "bad",
        },
    )
    assert bank_lint.lint_bank(doc) == [
        "one: invalid rubric_category 'invalid'",
        "rubric mix off: recall 0% (target 20% ±10%)",
        "rubric mix off: application 0% (target 50% ±10%)",
        "rubric mix off: analysis 0% (target 30% ±10%)",
        "policy: unknown key(s) unknown",
        "policy: pool 2 exceeds the 1 questions in the bank — nothing is held back",
        "policy: max_attempts must be a positive integer, got 0",
        "policy: pass_threshold must be a number, got True",
        "policy: mode must be one of exam, graded, practice, got 'bad'",
    ]


def test_loader_converts_and_forwards_import_command(monkeypatch):
    session = object()
    tenant_id, course_id = uuid4(), uuid4()
    doc = learning.BankDoc(
        "source",
        1,
        "chapter",
        2,
        [
            {"id": "one", "stem": "S?", "type": "single", "rubric_category": "recall",
             "weight": 2, "options": ["A", "B"], "correct": ["A"], "category": date(2026, 10, 17)},
            {"id": "two", "stem": "S?", "type": "single", "rubric_category": "recall",
             "options": ["A"], "correct": ["A"]},
        ],
        policy={"pool": None},
    )
    report = learning.BankImportReport(
        outcome="created", bank_id=uuid4(), version_id=uuid4(), content_hash="h",
        lint_violations=(), policy_proposals=(("pool", None),),
    )
    context = learning.BankAuditContext(
        tenant_id=tenant_id, actor_type="system", source_application="academy_cli"
    )
    seen = {}

    def owner(db, command, *, audit_context):
        seen.update(db=db, command=command, audit_context=audit_context)
        return report

    monkeypatch.setattr(learning, "import_bank_document_as_draft", owner)
    result = bank_loader.import_bank_as_draft(
        session, tenant_id=tenant_id, course_id=course_id, doc=doc, audit_context=context
    )
    assert result is report
    assert seen["db"] is session and seen["audit_context"] is context
    command = seen["command"]
    assert type(command) is learning.ImportBankDocumentAsDraftCommand
    assert command.tenant_id == tenant_id and command.course_id == course_id
    assert command.missing_question_weight == 1
    assert [q.weight for q in command.doc.questions] == [2, 1]
    assert command.doc.questions[0].category == "2026-10-17"
    assert command.doc.policy == {"pool": None}


def test_loader_preserves_owner_exception_without_rollback(monkeypatch):
    error = learning.BankImportRefused("draft exists")
    doc = learning.BankDoc(
        "source", None, "final", 1,
        [{"id": "q", "stem": "S?", "type": "single", "rubric_category": "recall",
          "options": [], "correct": [], "weight": 1}],
    )
    context = learning.BankAuditContext(
        tenant_id=uuid4(), actor_type="system", source_application="academy_cli"
    )

    def owner(db, command, *, audit_context):
        raise error

    monkeypatch.setattr(learning, "import_bank_document_as_draft", owner)
    with pytest.raises(learning.BankImportRefused) as raised:
        bank_loader.import_bank_as_draft(
            object(), tenant_id=context.tenant_id, course_id=uuid4(), doc=doc, audit_context=context
        )
    assert raised.value is error


def test_lint_cli_reports_parse_errors_and_counts(tmp_path, capsys):
    path = tmp_path / "bad.yaml"
    path.write_text("bank: {course: source, kind: final, questions: []}", encoding="utf-8")
    assert bank_lint._main([str(path)]) == 1
    assert capsys.readouterr().out == (f"FAIL {path}\n     bank has no questions\n\n0/1 bank(s) pass\n")
    assert bank_lint._main([]) == 2
    assert capsys.readouterr().err == "no bank files given\n"


def test_retired_writers_fail_closed_and_new_command_holds_document():
    with pytest.raises(learning.LegacyBankWriterRetired):
        learning.import_bank_with_activity(object(), object())
    with pytest.raises(learning.LegacyBankWriterRetired):
        learning.load_bank(
            object(), tenant_id=uuid4(), course_id=uuid4(),
            doc=learning.BankDoc("s", None, "final", 1, []), missing_question_weight=1,
        )
    document = bank_loader.bankdoc_to_document(
        learning.BankDoc(
            "source", 1, "chapter", 1,
            [{"id": "q", "stem": "S?", "type": "single", "rubric_category": "recall",
              "options": ["A"], "correct": ["A"], "weight": 1}],
            {"pool": None},
        ),
        missing_question_weight=1,
    )
    command = learning.ImportBankDocumentAsDraftCommand(
        tenant_id=uuid4(), course_id=uuid4(), doc=document, missing_question_weight=1
    )
    assert command.doc is document
    assert command.doc.policy["pool"] is None
    assert command.replace_imported_draft is False


def test_database_read_forwards_supplied_header_identity_without_inspection(
    monkeypatch,
):
    session = object()
    bank = SimpleNamespace(tenant_id=uuid4(), id=uuid4(), chapter_number=None, kind="final", version=2)
    raw = learning.BankDoc(
        "source",
        None,
        "final",
        2,
        [
            {
                "id": "one",
                "stem": "Q?",
                "type": "numeric",
                "options": None,
                "correct": None,
                "rubric_category": "analysis",
                "category": None,
                "explanation": None,
                "weight": 1,
            }
        ],
    )
    calls = []

    def owner(db, **kwargs):
        assert db is session
        assert kwargs["bank"] is bank
        calls.append(kwargs)
        return raw

    monkeypatch.setattr(bank_loader, "_decoded_doc_from_db", owner)
    result = bank_loader.doc_from_db(session, bank=bank, course_slug="source")
    assert result is raw
    assert calls == [{"bank": bank, "course_slug": "source"}]
    assert list(result.questions[0]) == [
        "id",
        "stem",
        "type",
        "options",
        "correct",
        "rubric_category",
        "category",
        "explanation",
        "weight",
    ]
    assert "competency" not in result.questions[0]
    assert result.questions[0]["options"] is None
    assert result.policy == {}


@pytest.fixture(autouse=True)
def _explicit_learning_policy_settings(monkeypatch):
    # Characterization data belongs to the product fixture, never the library.
    from app.services import settings_store

    original = settings_store.effective

    def effective(db, *, require_store=False):
        if require_store:
            return {
                "learning_missing_question_weight": 1,
                "learning_default_extra_attempts": 1,
                "learning_default_course_status": "published",
                "learning_default_course_listed": False,
                "learning_exam_seconds_per_question": 12.0,
                "learning_exam_near_chance_margin": 1.33,
                "learning_exam_fallback_score_floor": 1.0 / 3.0,
                "learning_exam_fallback_duration_seconds": 360.0,
            }
        return original(db)

    monkeypatch.setattr(settings_store, "effective", effective)
