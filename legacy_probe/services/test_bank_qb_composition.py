"""Pure QB composition tests: conversion, roles, audit, sitting, pins.

No real bank content is used; synthetic questions only. DB-bound
lifecycle behavior is covered by hosted tests (declared, not run here).
"""

from __future__ import annotations

import ast
from dataclasses import fields
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import dotmac_learning as learning
import pytest

from app import cli
from app.config import settings
from app.services import bank_authoring, bank_lint, bank_loader, entrance_exam, settings_store

ROOT = Path(__file__).resolve().parent.parent.parent


def _doc(**overrides):
    base = dict(
        course="source",
        chapter=3,
        kind="chapter",
        version=2,
        questions=[
            {
                "id": "q1",
                "stem": "S1?",
                "type": "single",
                "rubric_category": "recall",
                "options": ["A", "B"],
                "correct": ["A"],
                "category": date(2026, 10, 17),
                "weight": 2,
            },
            {
                "id": "q2",
                "stem": "S2?",
                "type": "single",
                "rubric_category": "application",
                "options": ["A", "B"],
                "correct": ["B"],
            },
        ],
        policy={"pool": 5, "max_attempts": 2},
    )
    base.update(overrides)
    return learning.BankDoc(**base)


# --- conversion ------------------------------------------------------------


def test_conversion_fills_missing_weight_and_keeps_policy():
    document = bank_loader.bankdoc_to_document(_doc(), missing_question_weight=3)
    assert type(document) is learning.BankDocument
    assert [q.id for q in document.questions] == ["q1", "q2"]
    assert document.questions[0].weight == 2
    assert document.questions[1].weight == 3
    assert document.questions[0].category == "2026-10-17"
    assert document.policy == {"pool": 5, "max_attempts": 2}
    assert document.version == 2


@pytest.mark.parametrize("weight", ["malformed", 1.9, True, 0, -1])
def test_conversion_refuses_bad_weight_naming_only_ext_id(weight):
    doc = _doc()
    doc.questions[0]["weight"] = weight
    with pytest.raises(bank_loader.BankDocumentConversionError, match="q1: invalid weight"):
        bank_loader.bankdoc_to_document(doc, missing_question_weight=1)


def test_conversion_refuses_missing_stem():
    doc = _doc()
    del doc.questions[0]["stem"]
    with pytest.raises(bank_loader.BankDocumentConversionError, match="missing or invalid stem"):
        bank_loader.bankdoc_to_document(doc, missing_question_weight=1)


# --- adapter boundary ------------------------------------------------------


def test_import_forwards_converted_document_and_context(monkeypatch):
    session = object()
    tenant_id, course_id = uuid4(), uuid4()
    context = bank_authoring.cli_bank_audit_context(tenant_id=tenant_id)
    report = learning.BankImportReport(
        outcome="created", bank_id=uuid4(), version_id=uuid4(), content_hash="h",
        lint_violations=(), policy_proposals=(("pool", 5),),
    )
    seen = {}

    def owner(db, command, *, audit_context):
        seen.update(db=db, command=command, audit_context=audit_context)
        return report

    monkeypatch.setattr(learning, "import_bank_document_as_draft", owner)
    monkeypatch.setattr(
        settings_store, "effective",
        lambda db, require_store=False: {"learning_missing_question_weight": 3},
    )
    result = bank_loader.import_bank_as_draft(
        session, tenant_id=tenant_id, course_id=course_id, doc=_doc(),
        audit_context=context, replace_imported_draft=True,
    )
    assert result is report
    assert seen["db"] is session
    assert seen["audit_context"] is context
    command = seen["command"]
    assert type(command) is learning.ImportBankDocumentAsDraftCommand
    assert command.tenant_id == tenant_id and command.course_id == course_id
    assert command.missing_question_weight == 3
    assert command.replace_imported_draft is True
    assert type(command.doc) is learning.BankDocument
    assert not hasattr(command, "terms")


def test_retired_learning_writers_fail_closed():
    with pytest.raises(learning.LegacyBankWriterRetired):
        learning.load_bank(object(), tenant_id=uuid4(), course_id=uuid4(), doc=_doc(), missing_question_weight=1)
    with pytest.raises(learning.LegacyBankWriterRetired):
        learning.import_bank_with_activity(object(), object())


def test_academy_never_calls_retired_writers_or_reads_question_rows():
    retired = {"load_bank", "import_bank_with_activity"}
    for relative in (
        "app/services/bank_loader.py",
        "app/services/bank_authoring.py",
        "app/cli.py",
    ):
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"), filename=relative)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in retired, f"{relative} calls retired {node.func.attr}"
    for relative in ("app/web/learn.py", "app/web/apply.py", "app/services/entrance_exam.py"):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "select(Question)" not in text, relative
        assert "from app.models.assessment import" not in text or "Question," not in text.split(
            "from app.models.assessment import"
        )[1].splitlines()[0], relative


# --- YAML transport ----------------------------------------------------------


def test_encode_round_trips_structure_with_provenance():
    document = bank_loader.bankdoc_to_document(_doc(), missing_question_weight=1)
    text = bank_lint.encode_bank_document(
        document, provenance={"version_id": "v1", "content_hash": "abc", "bank_id": "b1"}
    )
    assert text.startswith("# bank_id: b1\n# content_hash: abc\n# version_id: v1\n")
    parsed = bank_lint.parse_bank_text(text)
    assert parsed.course == "source" and parsed.kind == "chapter" and parsed.chapter == 3
    assert [q["id"] for q in parsed.questions] == ["q1", "q2"]
    assert parsed.policy == {"pool": 5, "max_attempts": 2}


# --- roles -------------------------------------------------------------------


def test_bank_role_defaults_match_adopted_matrix():
    assert settings.bank_author_roles == ("instructor", "admin")
    assert settings.bank_publish_roles == ("admin",)
    assert settings.bank_repoint_roles == ("admin",)
    assert settings.bank_export_roles == ("admin",)
    assert settings.bank_retire_roles == ("admin",)
    for key in bank_authoring.ROLE_KEYS:
        assert key in settings_store.KNOWN_KEYS


def test_roles_allow_and_require(monkeypatch):
    assert bank_authoring.roles_allow({"admin"}, ("admin",)) is True
    assert bank_authoring.roles_allow({"instructor"}, ("admin",)) is False
    assert bank_authoring.roles_allow({"admin"}, ()) is False
    tenant_id, person_id = uuid4(), uuid4()
    monkeypatch.setattr(
        bank_authoring, "effective",
        lambda db, require_store=False: {"bank_publish_roles": ("admin",)},
    )
    monkeypatch.setattr(bank_authoring, "role_slugs", lambda db, t, p: {"instructor"})
    with pytest.raises(bank_authoring.BankPermissionDenied):
        bank_authoring.require_bank_role(object(), tenant_id=tenant_id, person_id=person_id, key="bank_publish_roles")
    monkeypatch.setattr(bank_authoring, "role_slugs", lambda db, t, p: {"admin"})
    bank_authoring.require_bank_role(object(), tenant_id=tenant_id, person_id=person_id, key="bank_publish_roles")


# --- audit contexts ------------------------------------------------------------


def test_audit_contexts_from_session_and_cli():
    tenant_id = uuid4()
    account = SimpleNamespace(id=uuid4(), email="instructor@example.test")
    web = bank_authoring.web_bank_audit_context(tenant_id=tenant_id, account=account)
    assert (web.tenant_id, web.actor_type, web.source_application) == (tenant_id, "user", "dotmac_academy_app")
    assert web.actor_id == str(account.id) and web.actor_party_id == account.id
    assert web.actor_label == "instructor@example.test"
    long_account = SimpleNamespace(id=uuid4(), email="x" * 200)
    assert bank_authoring.web_bank_audit_context(tenant_id=tenant_id, account=long_account).actor_label is None
    cli_context = bank_authoring.cli_bank_audit_context(tenant_id=tenant_id)
    assert (cli_context.actor_type, cli_context.source_application) == ("system", "academy_cli")
    assert cli_context.actor_party_id is None


# --- sitting projection --------------------------------------------------------


def test_sitting_projection_cannot_carry_keys():
    assert {f.name for f in fields(learning.SittingQuestion)} == {"ext_id", "stem", "type", "options"}
    rows = bank_authoring.sitting_dicts(
        (learning.SittingQuestion(ext_id="q1", stem="S?", type="single", options=("A", "B")),)
    )
    assert rows == [{"ext_id": "q1", "stem": "S?", "type": "single", "options": ["A", "B"]}]
    assert "correct" not in rows[0] and "explanation" not in rows[0]


# --- entrance pins ---------------------------------------------------------------


class _FakeDB:
    def __init__(self):
        self.flushed = 0

    def flush(self):
        self.flushed += 1

    def get(self, _model, _key):
        return None


def _applicant(**overrides):
    base = dict(
        id=uuid4(), tenant_id=uuid4(), cohort_id=None, assessment_bank_id=None,
        assessment_bank_version_id=None, assessment_started_at=None,
        assessment_elapsed_seconds=0, assessment_taken_at=None, assessment_answers=None,
        assessment_score=0.5, assessment_level="x", assessment_profile={},
        assessment_valid=True, assessment_invalid_reason=None, assessment_time_exceeded=True,
        assessment_deadline=None, assessment_erp_synced_at=None, assessment_reset_count=0,
        assessment_token_hash=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_pin_wins_without_current_lookup(monkeypatch):
    pin, bank_id = uuid4(), uuid4()
    applicant = _applicant(assessment_bank_version_id=pin)
    monkeypatch.setattr(
        learning, "get_bank_fact", lambda *a, **k: pytest.fail("pin must short-circuit")
    )
    assert entrance_exam.effective_bank_version_id(
        object(), tenant_id=applicant.tenant_id, applicant=applicant, bank_id=bank_id
    ) == pin


def test_current_then_legacy_then_retired(monkeypatch):
    bank_id, current = uuid4(), uuid4()
    applicant = _applicant()
    fact = SimpleNamespace(current_version_id=current)
    monkeypatch.setattr(learning, "get_bank_fact", lambda *a, **k: fact)
    assert entrance_exam.effective_bank_version_id(
        object(), tenant_id=applicant.tenant_id, applicant=applicant, bank_id=bank_id
    ) == current
    monkeypatch.setattr(learning, "get_bank_fact", lambda *a, **k: SimpleNamespace(current_version_id=None))
    monkeypatch.setattr(learning, "bank_version_history", lambda *a, **k: ())
    assert entrance_exam.effective_bank_version_id(
        object(), tenant_id=applicant.tenant_id, applicant=applicant, bank_id=bank_id
    ) is None
    monkeypatch.setattr(learning, "bank_version_history", lambda *a, **k: (object(),))
    with pytest.raises(Exception, match="No published entrance"):
        entrance_exam.effective_bank_version_id(
            object(), tenant_id=applicant.tenant_id, applicant=applicant, bank_id=bank_id
        )


def test_start_stamps_pin_once_and_reset_clears_it(monkeypatch):
    db = _FakeDB()
    bank_id, version_id = uuid4(), uuid4()
    applicant = _applicant()
    monkeypatch.setattr(entrance_exam, "resolve_bank_id", lambda db, applicant: bank_id)
    monkeypatch.setattr(
        entrance_exam, "effective_bank_version_id",
        lambda db, tenant_id, applicant, bank_id: version_id,
    )
    now = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
    entrance_exam.start_exam(db, applicant=applicant, now=now)
    assert applicant.assessment_bank_version_id == version_id
    assert applicant.assessment_started_at == now
    # Second start keeps the original pin and clock.
    monkeypatch.setattr(
        entrance_exam, "effective_bank_version_id",
        lambda *a, **k: pytest.fail("started sitting must not re-pin"),
    )
    entrance_exam.start_exam(db, applicant=applicant, now=now)
    assert applicant.assessment_bank_version_id == version_id
    entrance_exam.reset_exam(db, applicant=applicant)
    assert applicant.assessment_bank_version_id is None
    assert applicant.assessment_started_at is None


# --- CLI wiring (AST; main() builds the parser inline) -------------------------


def test_cli_registers_import_export_and_audit_commands():
    text = (ROOT / "app" / "cli.py").read_text(encoding="utf-8")
    assert '"load-banks"' in text and '"import-banks-as-draft"' in text
    assert '"export-bank"' in text and '"audit-banks"' in text
    assert "func=_load_banks" in text and "func=_export_bank" in text and "func=_audit_banks" in text
    assert "import_bank_with_activity" not in text
    assert callable(cli._load_banks) and callable(cli._export_bank) and callable(cli._audit_banks)


# --- instructor surface (AST; module import is a pre-existing parent blocker) --


def test_instructor_bank_routes_and_role_gates_exist():
    text = (ROOT / "app" / "web" / "instructor.py").read_text(encoding="utf-8")
    tree = ast.parse(text)
    paths = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                if isinstance(decorator, ast.Call) and decorator.args and isinstance(decorator.args[0], ast.Constant):
                    paths.add(str(decorator.args[0].value))
    expected = {
        "/courses/{course_id}/banks",
        "/courses/{course_id}/banks/drafts",
        "/courses/{course_id}/banks/import",
        "/courses/{course_id}/banks/{bank_id}",
        "/courses/{course_id}/banks/{bank_id}/draft/questions",
        "/courses/{course_id}/banks/{bank_id}/draft/questions/{ext_id}/delete",
        "/courses/{course_id}/banks/{bank_id}/draft/discard",
        "/courses/{course_id}/banks/{bank_id}/draft/lint",
        "/courses/{course_id}/banks/{bank_id}/draft/publish",
        "/courses/{course_id}/banks/{bank_id}/versions/{version_id}/retire",
        "/courses/{course_id}/banks/{bank_id}/versions/{version_id}/repoint",
        "/courses/{course_id}/banks/{bank_id}/versions/{version_id}/export",
    }
    assert expected <= paths
    for gate in (
        "require_bank_author_access", "require_bank_publish_access", "require_bank_repoint_access",
        "require_bank_export_access", "require_bank_retire_access",
    ):
        assert gate in text
    assert (ROOT / "templates" / "instructor" / "banks.html").exists()
    assert (ROOT / "templates" / "instructor" / "bank.html").exists()


# --- B-1: pooled re-render serves the open attempt's pin --------------------


def test_activity_sitting_uses_attempt_pin_over_activity_resolution(monkeypatch):
    # After a publish/repoint the activity resolves to the NEW version,
    # but an open attempt's pin is the OLD version Learning will grade.
    old_pin, new_version = uuid4(), uuid4()
    seen = {}

    def sitting(db, *, tenant_id, version_id, sitter_id, only_ext_ids=None):
        seen["version_id"] = version_id
        seen["only_ext_ids"] = only_ext_ids
        return (learning.SittingQuestion(ext_id="q1", stem="old stem?", type="single", options=("A", "B")),)

    monkeypatch.setattr(learning, "sitting_questions", sitting)
    monkeypatch.setattr(
        bank_authoring, "resolve_activity_version_id",
        lambda db, tenant_id, activity: new_version,
    )
    activity = SimpleNamespace(bank_id=uuid4(), bank_version_id=new_version)
    sitter = SimpleNamespace(id=uuid4())
    version_id, rows = bank_authoring.activity_sitting(
        object(), tenant_id=uuid4(), activity=activity, sitter=sitter,
        only_ext_ids=["q1"], pinned_version_id=old_pin,
    )
    assert version_id == old_pin
    assert seen == {"version_id": old_pin, "only_ext_ids": ["q1"]}
    assert rows == [{"ext_id": "q1", "stem": "old stem?", "type": "single", "options": ["A", "B"]}]


def test_activity_sitting_without_pin_resolves_activity(monkeypatch):
    resolved = uuid4()
    seen = {}
    monkeypatch.setattr(
        learning, "sitting_questions",
        lambda db, *, tenant_id, version_id, sitter_id, only_ext_ids=None: seen.update(version_id=version_id) or (),
    )
    monkeypatch.setattr(
        bank_authoring, "resolve_activity_version_id", lambda db, tenant_id, activity: resolved
    )
    version_id, rows = bank_authoring.activity_sitting(
        object(), tenant_id=uuid4(),
        activity=SimpleNamespace(bank_id=uuid4(), bank_version_id=None),
        sitter=SimpleNamespace(id=uuid4()),
    )
    assert version_id == resolved and seen["version_id"] == resolved and rows == []


def test_learn_get_rerenders_pooled_attempt_from_its_pin():
    # AST/source seam (DB route cannot run locally): the pooled GET must
    # read the open attempt and thread its pin into the sitting, using
    # activity resolution only in the new-draw branch.
    text = (ROOT / "app" / "web" / "learn.py").read_text(encoding="utf-8")
    get_segment = text.split("def activity(")[1].split("async def submit(")[0]
    assert "get_open_attempt(" in get_segment
    assert "existing.bank_version_id" in get_segment
    assert "pinned_version_id=pinned_version_id" in get_segment
    # New-draw helpers run only when no open attempt exists.
    assert get_segment.index("if existing is not None:") < get_segment.index("activity_ext_ids(")
    assert get_segment.index("if existing is not None:") < get_segment.index("open_or_create_attempt(")
    # Retired-pin refusal inside Learning stays the declared B2 behavior.
    assert "already-declared B2" in get_segment


# --- A-P2-2: malformed web YAML uploads never 500 ----------------------------


@pytest.mark.parametrize(
    "text",
    [
        "not-a-mapping\n",
        "bank: [1,2\n",
        "other:\n  course: SECRET-COURSE\n",
        "bank:\n  kind: chapter\n  questions: []\n",
    ],
)
def test_import_yaml_as_draft_maps_malformed_uploads_content_free(monkeypatch, text):
    monkeypatch.setattr(
        bank_authoring, "import_bank_as_draft",
        lambda *args, **kwargs: pytest.fail("owner called on malformed upload"),
    )
    with pytest.raises(bank_authoring.BadRequestError) as excinfo:
        bank_authoring.import_yaml_as_draft(
            object(), tenant_id=uuid4(), course_id=uuid4(), text=text,
            context=bank_authoring.cli_bank_audit_context(tenant_id=uuid4()),
        )
    assert str(excinfo.value) == bank_authoring.BANK_UPLOAD_PARSE_ERROR
    assert "SECRET" not in str(excinfo.value)
    assert "not-a-mapping" not in str(excinfo.value)


def test_decode_bank_text_maps_non_utf8_content_free():
    with pytest.raises(bank_authoring.BadRequestError) as excinfo:
        bank_authoring.decode_bank_text(b"\xff\xfeSECRET-KEY-BYTES")
    assert str(excinfo.value) == bank_authoring.BANK_UPLOAD_PARSE_ERROR
    assert "SECRET" not in str(excinfo.value)
    assert bank_authoring.decode_bank_text("bank: {}\n".encode()) == "bank: {}\n"


def test_instructor_import_decodes_inside_handled_region():
    text = (ROOT / "app" / "web" / "instructor.py").read_text(encoding="utf-8")
    segment = text.split("async def bank_import_yaml(")[1].split("@router.get(")[0]
    assert "decode_bank_text(await upload.read())" in segment
    assert segment.index("try:") < segment.index("decode_bank_text(")
    assert segment.index("decode_bank_text(") < segment.index("except _BANK_ERRORS")
    assert '.decode("utf-8")' not in segment
    # Role gate is unchanged.
    assert "require_bank_author_access" in segment


def test_draft_question_from_lines():
    write = bank_authoring.draft_question_from_lines(
        ext_id=" q9 ", stem="S?", type_="single", rubric_category="recall",
        options_text="A\n\nB\n", correct_text="A\n", category=" numeracy ",
        explanation="because", weight=2,
    )
    assert write.ext_id == "q9" and write.options == ["A", "B"] and write.correct == ["A"]
    assert write.category == "numeracy" and write.weight == 2
    with pytest.raises(Exception, match="positive whole number"):
        bank_authoring.draft_question_from_lines(
            ext_id="q", stem="S", type_="single", rubric_category="recall",
            options_text="", correct_text="", category="", explanation="", weight=0,
        )
