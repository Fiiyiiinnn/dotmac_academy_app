"""Invoke the real CLI flow; stubs prove adapter transaction boundaries only.

Import-as-draft semantics: the CLI resolves the course, converts the
decoded YAML through the Academy adapter, and delegates the draft write
to Learning. It never publishes and never touches activities. Domain
SQL/failure-stage characterization belongs to the hosted module tests.
"""

import argparse
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import dotmac_learning as learning
import pytest
import yaml

from app import cli
from app.services import settings_store


class _Scalars:
    def __init__(self, value):
        self._value = value

    def first(self):
        return self._value


class CallerSession:
    """Records transaction events; serves queued course lookups only."""

    def __init__(self, courses=()):
        self.events = []
        self._courses = list(courses)

    def scalars(self, _statement):
        return _Scalars(self._courses.pop(0) if self._courses else None)

    def commit(self):
        self.events.append("commit")

    def rollback(self):
        self.events.append("rollback")

    def __getattr__(self, name):
        raise AssertionError(f"CLI must delegate writes beyond course lookup: {name}")


@pytest.fixture
def caller(monkeypatch):
    bank_course = SimpleNamespace(id=uuid4(), slug="source")
    session = CallerSession(courses=[bank_course] * 8)
    tenant = SimpleNamespace(id=uuid4(), slug="school")

    @contextmanager
    def tenant_session(slug):
        assert slug == tenant.slug
        try:
            yield session, tenant
        except BaseException:
            session.rollback()
            raise

    monkeypatch.setattr(cli, "_tenant_session", tenant_session)

    def configured(db, *, require_store=False):
        assert db is session
        assert require_store is True
        return {"learning_missing_question_weight": 3}

    monkeypatch.setattr(settings_store, "effective", configured)
    return session, tenant, bank_course


def args(path, **extra):
    return argparse.Namespace(
        tenant_slug="school", banks_dir=str(path), replace_imported_draft=False, **extra
    )


def write_bank(path, *, course="source", weight="1", policy="", questions=10):
    text = f"bank:\n  course: {course}\n  chapter: 3\n  kind: chapter\n" "  version: '2'\n  questions:\n"
    categories = (["recall"] * 2 + ["application"] * 5 + ["analysis"] * 3)[:questions]
    for index, category in enumerate(categories):
        weight_line = f"      weight: {weight}\n" if weight is not None else ""
        text += (
            f"    - id: q{index}\n      stem: True?\n      type: truefalse\n"
            f"      rubric_category: {category}\n{weight_line}"
            "      category: 2026-10-17\n      options: [true, false]\n"
            "      correct: [true]\n"
        )
    if questions == 0:
        text = f"bank:\n  course: {course}\n  kind: final\n  questions: []\n"
    path.write_text(text + policy, encoding="utf-8")


def _report(**overrides):
    base = dict(
        outcome="created", bank_id=uuid4(), version_id=uuid4(), content_hash="h" * 8,
        lint_violations=(), policy_proposals=(),
    )
    base.update(overrides)
    return learning.BankImportReport(**base)


def test_cli_imports_sorted_files_as_drafts_and_reports_proposals(tmp_path, caller, monkeypatch, capsys):
    session, tenant, course = caller
    write_bank(tmp_path / "b.yaml", policy="  policy: {max_attempts: 2, mode: practice}\n")
    write_bank(tmp_path / "a.yaml", weight=None, policy="  policy: {pool: 5}\n")
    calls = []
    bank_id, version_id = uuid4(), uuid4()

    def owner(db, command, *, audit_context):
        assert db is session
        assert type(command) is learning.ImportBankDocumentAsDraftCommand
        assert command.tenant_id == tenant.id and command.course_id == course.id
        assert command.missing_question_weight == 3
        assert command.replace_imported_draft is False
        assert audit_context.actor_type == "system"
        assert audit_context.source_application == "academy_cli"
        doc = command.doc
        assert type(doc) is learning.BankDocument
        assert doc.questions[0].category == "2026-10-17"
        assert doc.questions[0].options == [True, False]
        calls.append(doc)
        return _report(
            bank_id=bank_id, version_id=version_id,
            policy_proposals=tuple(sorted(doc.policy.items())),
        )

    monkeypatch.setattr(learning, "import_bank_document_as_draft", owner)
    cli._load_banks(args(tmp_path))
    assert calls[0].questions[0].weight == 3  # a.yaml: missing weight filled from settings
    assert calls[1].questions[0].weight == 1
    assert session.events == ["commit", "commit"]
    assert capsys.readouterr().out == (
        f"Imported a.yaml as draft: outcome=created bank={bank_id} version={version_id} "
        "questions=10 lint_violations=0, policy proposals NOT applied: pool=5\n"
        f"Imported b.yaml as draft: outcome=created bank={bank_id} version={version_id} "
        "questions=10 lint_violations=0, policy proposals NOT applied: max_attempts=2 mode=practice\n"
        "Done — 2/2 bank(s) imported as drafts (none published).\n"
    )


def test_lint_failing_bank_still_imports_as_draft(tmp_path, caller, monkeypatch, capsys):
    session, _tenant, _course = caller
    write_bank(tmp_path / "bad.yaml", questions=0)
    bank_id, version_id = uuid4(), uuid4()

    def owner(db, command, *, audit_context):
        return _report(bank_id=bank_id, version_id=version_id, lint_violations=("bank has no questions",))

    monkeypatch.setattr(learning, "import_bank_document_as_draft", owner)
    cli._load_banks(args(tmp_path))
    assert session.events == ["commit"]
    assert capsys.readouterr().out == (
        f"Imported bad.yaml as draft: outcome=created bank={bank_id} version={version_id} "
        "questions=0 lint_violations=1\n"
        "  - bank has no questions\n"
        "Done — 1/1 bank(s) imported as drafts (none published).\n"
    )


def test_unknown_course_skips_before_owner(tmp_path, caller, monkeypatch, capsys):
    session, _tenant, _course = caller
    session._courses = [None]
    write_bank(tmp_path / "unknown.yaml", course="missing")
    monkeypatch.setattr(
        learning, "import_bank_document_as_draft",
        lambda *a, **k: pytest.fail("owner must not run for unknown course"),
    )
    cli._load_banks(args(tmp_path))
    assert session.events == []
    assert capsys.readouterr().out == (
        "SKIP unknown.yaml: course 'missing' not found for tenant 'school'\n"
        "Done — 0/1 bank(s) imported as drafts (none published).\n"
    )


def test_malformed_weight_refused_at_conversion_with_rollback(tmp_path, caller, monkeypatch, capsys):
    session, _tenant, _course = caller
    write_bank(tmp_path / "bad.yaml", weight="malformed")
    monkeypatch.setattr(
        learning, "import_bank_document_as_draft",
        lambda *a, **k: pytest.fail("conversion must refuse first"),
    )
    cli._load_banks(args(tmp_path))
    assert session.events == ["rollback"]
    assert capsys.readouterr().out == (
        "SKIP bad.yaml: q0: invalid weight\n"
        "Done — 0/1 bank(s) imported as drafts (none published).\n"
    )


def test_import_refusal_commits_refusal_audit_and_continues(tmp_path, caller, monkeypatch, capsys):
    session, _tenant, _course = caller
    write_bank(tmp_path / "a.yaml")
    write_bank(tmp_path / "b.yaml")
    bank_id, version_id = uuid4(), uuid4()
    calls = []

    def owner(db, command, *, audit_context):
        calls.append(command)
        if len(calls) == 1:
            raise learning.BankImportRefused("a draft already exists for this bank (origin=ui)")
        return _report(bank_id=bank_id, version_id=version_id)

    monkeypatch.setattr(learning, "import_bank_document_as_draft", owner)
    cli._load_banks(args(tmp_path))
    # Refusal commits (audit evidence), success commits: two commits, no rollback.
    assert session.events == ["commit", "commit"]
    assert capsys.readouterr().out == (
        "REFUSED a.yaml: a draft already exists for this bank (origin=ui)\n"
        f"Imported b.yaml as draft: outcome=created bank={bank_id} version={version_id} "
        "questions=10 lint_violations=0\n"
        "Done — 1/2 bank(s) imported as drafts (none published).\n"
    )


def test_parse_error_skips_file_without_owner(tmp_path, caller, monkeypatch, capsys):
    session, _tenant, _course = caller
    (tmp_path / "bad.yaml").write_text(
        "bank: {course: source, kind: final, version: bad, questions: []}", encoding="utf-8"
    )
    monkeypatch.setattr(
        learning, "import_bank_document_as_draft",
        lambda *a, **k: pytest.fail("decode must run first"),
    )
    cli._load_banks(args(tmp_path))
    assert session.events == []
    captured = capsys.readouterr()
    assert captured.out == (
        "SKIP bad.yaml: could not parse bank document\n"
        "Done — 0/1 bank(s) imported as drafts (none published).\n"
    )
    assert captured.err == ""


@pytest.mark.parametrize(
    "payload",
    [
        b"bank:\n  correct: [PRIVATE_ANSWER_KEY_PARSE_7c93, }\n",
        b"bank:\n  correct: PRIVATE_ANSWER_KEY_PARSE_7c93\xff\n",
    ],
    ids=["malformed-yaml", "invalid-utf8"],
)
def test_parse_failure_hides_source_and_continues(tmp_path, caller, monkeypatch, capsys, payload):
    from app.services.bank_loader import parse_bank

    session, _tenant, _course = caller
    bad_path = tmp_path / "a.yaml"
    bad_path.write_bytes(payload)
    write_bank(tmp_path / "b.yaml")
    # Exercise the genuine decoder; malformed YAML includes the answer key
    # in its exception's source excerpt before the CLI suppresses that detail.
    with pytest.raises((UnicodeDecodeError, yaml.YAMLError)) as failure:
        parse_bank(bad_path)
    raw_error = str(failure.value)
    if payload.isascii():
        assert "PRIVATE_ANSWER_KEY_PARSE_7c93" in raw_error
        assert "[PRIVATE_ANSWER_KEY_PARSE_7c93," in raw_error

    lookups = []
    course_lookup = session.scalars

    def scalars(statement):
        lookups.append(statement)
        return course_lookup(statement)

    monkeypatch.setattr(session, "scalars", scalars)
    calls = []
    report = _report()

    def owner(db, command, *, audit_context):
        assert db is session
        assert session.events == []  # The failed parse neither commits nor rolls back.
        assert len(lookups) == 1  # Only the valid bank reaches the course lookup.
        assert command.doc.course == "source"
        calls.append(command)
        return report

    monkeypatch.setattr(learning, "import_bank_document_as_draft", owner)
    cli._load_banks(args(tmp_path))
    assert len(calls) == 1
    assert session.events == ["commit"]
    captured = capsys.readouterr()
    assert captured.out == (
        "SKIP a.yaml: could not parse bank document\n"
        f"Imported b.yaml as draft: outcome=created bank={report.bank_id} "
        f"version={report.version_id} questions=10 lint_violations=0\n"
        "Done — 1/2 bank(s) imported as drafts (none published).\n"
    )
    assert captured.err == ""
    output = captured.out + captured.err
    assert "PRIVATE_ANSWER_KEY_PARSE_7c93" not in output
    assert raw_error not in output
    assert "correct:" not in output


def test_empty_and_missing_directories_keep_source_messages(tmp_path, caller, capsys):
    session, _tenant, _course = caller
    cli._load_banks(args(tmp_path))
    assert capsys.readouterr().out == f"No *.yaml files found in {tmp_path}\n"
    missing = tmp_path / "missing"
    with pytest.raises(SystemExit, match="Banks directory not found"):
        cli._load_banks(args(missing))
    assert session.events == ["rollback"]


def test_policy_store_failure_refuses_owner_and_rolls_back(tmp_path, caller, monkeypatch):
    session, _tenant, _course = caller
    write_bank(tmp_path / "source.yaml")

    def unavailable(db, *, require_store=False):
        assert db is session
        assert require_store is True
        raise RuntimeError("policy store unavailable")

    monkeypatch.setattr(settings_store, "effective", unavailable)
    monkeypatch.setattr(
        learning, "import_bank_document_as_draft",
        lambda *a, **k: pytest.fail("policy read must succeed first"),
    )
    with pytest.raises(RuntimeError, match="policy store unavailable"):
        cli._load_banks(args(tmp_path))
    assert session.events == ["rollback"]
