"""Product boundary canaries, not installed PostgreSQL/Kernel identity proof.

The public owner operations are observed, never replaced with a shadow database.
Existing legacy admin-session integration tests remain unchanged separately.
"""

import math
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services import lab_content


class ObservedSession:
    def __init__(self, rows=()):
        self.rows = iter(rows)
        self.events = []
        self.added = []
        self.settings_read = False

    @property
    @contextmanager
    def no_autoflush(self):
        assert not self.settings_read
        self.settings_read = True
        self.events.append("settings-enter")
        try:
            yield
        finally:
            self.events.append("settings-exit")
            self.settings_read = False

    def scalars(self, statement):
        assert not self.settings_read
        self.events.append("template-query")
        return SimpleNamespace(first=lambda: next(self.rows))

    def add(self, row):
        self.events.append("template-add")
        self.added.append(row)

    def flush(self):
        assert not self.settings_read
        self.events.append("full-flush")


def _lab(tmp_path, name="a", threshold="missing", title="Lab"):
    path = tmp_path / name
    path.mkdir()
    limits = "" if threshold == "missing" else f"limits:\n  pass_threshold: {threshold}\n"
    (path / "lab.yaml").write_text(f"slug: {name}\ntitle: {title}\n{limits}")
    (path / "topo.clab.yml").write_text("name: lab\n")
    (path / "instructions.md").write_text("# Lab\n")
    return path


def _cfg():
    return {
        "learning_lab_pass_threshold": 0.83,
        "learning_default_activity_assessment_mode": "exam",
        "learning_default_activity_grading": "manual",
        "learning_default_activity_weight": 2.5,
        "learning_default_activity_scoring_policy": "best",
        "learning_default_attempt_cooldown_seconds": 0,
    }


def _observe(monkeypatch, db, config=None):
    identities, creates, patches, keys = [], [], [], []

    class ExactKeys(dict):
        def __getitem__(self, key):
            keys.append(key)
            return super().__getitem__(key)

    cfg = ExactKeys(_cfg() if config is None else config)

    def effective(session, *, require_store):
        assert session is db and require_store is True and db.settings_read
        db.events.append("settings-query")
        return cfg

    def create(session, payload):
        assert session is db and not db.settings_read
        creates.append(payload)
        db.events.append("owner-create")
        session.flush()
        identity = uuid4()
        identities.append(identity)
        return identity

    def patch(session, payload):
        assert session is db and not db.settings_read
        patches.append(payload)
        db.events.append("owner-patch")
        return False  # Missing Activity remains a tolerated product outcome.

    monkeypatch.setattr(lab_content, "effective", effective)
    monkeypatch.setattr(lab_content.learning, "create_activity_identity", create)
    monkeypatch.setattr(lab_content.learning, "stage_activity_patch", patch)
    return identities, creates, patches, keys


def _import(db, tmp_path):
    return lab_content.import_labs(db, tenant_id=uuid4(), course_id=uuid4(), labs_dir=tmp_path)


def _existing(path, *, changed):
    spec = lab_content.parse_lab(path)
    html = lab_content.md.markdown(spec.instructions_md, extensions=["tables", "fenced_code"])
    return SimpleNamespace(
        activity_id=uuid4(),
        version=4,
        source_hash="old" if changed else lab_content._source_hash(spec, html),
        # The product also reconciles the stored device-access block on an unchanged hash.
        access=spec.access,
    )


def test_new_missing_threshold_uses_exact_settings_once_and_owner_flush_before_template(monkeypatch, tmp_path):
    _lab(tmp_path)
    _lab(tmp_path, "b")
    db = ObservedSession([None, None])
    identities, creates, patches, keys = _observe(monkeypatch, db)
    result = _import(db, tmp_path)
    assert result == db.added and len(result) == 2 and patches == []
    assert [row.activity_id for row in result] == identities
    assert [payload.pass_threshold for payload in creates] == [0.83, 0.83]
    assert all(
        (payload.type, payload.assessment_mode, payload.grading, payload.weight) == ("lab", "exam", "manual", 2.5)
        for payload in creates
    )
    assert all(
        (payload.scoring_policy, payload.min_attempt_interval_seconds) == ("best", 0)
        for payload in creates
    )
    assert all(row.limits == {} and row.version == 1 for row in result)
    assert db.events == [
        "settings-enter",
        "settings-query",
        "settings-exit",
        "template-query",
        "owner-create",
        "full-flush",
        "template-add",
        "template-query",
        "owner-create",
        "full-flush",
        "template-add",
        "full-flush",
    ]
    assert keys == [
        "learning_lab_pass_threshold",
        "learning_default_activity_assessment_mode",
        "learning_default_activity_grading",
        "learning_default_activity_weight",
        "learning_default_activity_scoring_policy",
        "learning_default_attempt_cooldown_seconds",
        "learning_lab_pass_threshold",
        "learning_default_activity_assessment_mode",
        "learning_default_activity_grading",
        "learning_default_activity_weight",
        "learning_default_activity_scoring_policy",
        "learning_default_attempt_cooldown_seconds",
    ]


@pytest.mark.parametrize("changed", [True, False])
@pytest.mark.parametrize(
    "literal, expected",
    [
        ("0", 0.0),
        ("-1", -1.0),
        ("2", 2.0),
        ('"0.4"', 0.4),
        ("true", 1.0),
        (".inf", math.inf),
        ("-.inf", -math.inf),
        (".nan", math.nan),
    ],
)
def test_explicit_threshold_existing_changed_or_unchanged_never_reads_settings(
    monkeypatch, tmp_path, changed, literal, expected
):
    path = _lab(tmp_path, threshold=literal)
    template = _existing(path, changed=changed)
    db = ObservedSession([template])
    _, creates, patches, keys = _observe(monkeypatch, db)
    result = _import(db, tmp_path)
    assert result[0] is template and creates == [] and keys == []
    assert db.events == (
        ["template-query", "owner-patch", "full-flush"] if changed else ["template-query", "full-flush"]
    )
    assert template.version == (5 if changed else 4)
    if changed:
        assert patches[0].model_fields_set == {"id", "tenant_id", "title", "chapter_number", "pass_threshold"}
        assert patches[0].id == template.activity_id
        assert repr(patches[0].pass_threshold) == repr(expected)
        # Even a missing Activity must not prevent product Template versioning.
        assert template.limits["pass_threshold"] == lab_content.parse_lab(path).limits["pass_threshold"] or math.isnan(
            expected
        )


@pytest.mark.parametrize("literal, error", [("null", TypeError), ('"invalid"', ValueError)])
def test_bad_explicit_threshold_fails_before_template_or_settings(monkeypatch, tmp_path, literal, error):
    _lab(tmp_path, threshold=literal)
    db = ObservedSession()
    _, creates, patches, keys = _observe(monkeypatch, db)
    with pytest.raises(error):
        _import(db, tmp_path)
    assert db.events == [] and creates == patches == keys == []


def test_settings_failure_for_missing_threshold_is_before_template_and_keeps_caller_partial_state(
    monkeypatch, tmp_path
):
    _lab(tmp_path)
    db = ObservedSession()
    caller = object()
    db.added.append(caller)

    def refuse(session, *, require_store):
        assert session is db and require_store and db.settings_read
        raise RuntimeError("store refused")

    monkeypatch.setattr(lab_content, "effective", refuse)
    with pytest.raises(RuntimeError, match="store refused"):
        _import(db, tmp_path)
    assert db.events == ["settings-enter", "settings-exit"]
    assert db.added == [caller]


def test_new_explicit_threshold_reads_settings_after_template_query(monkeypatch, tmp_path):
    _lab(tmp_path, threshold="0")
    db = ObservedSession([None])
    _, creates, _, keys = _observe(monkeypatch, db)
    _import(db, tmp_path)
    assert creates[0].pass_threshold == 0.0
    assert "learning_lab_pass_threshold" not in keys
    assert db.events[:4] == ["template-query", "settings-enter", "settings-query", "settings-exit"]


def test_empty_directory_still_full_flush_without_settings_or_owner(monkeypatch, tmp_path):
    db = ObservedSession()
    _, creates, patches, keys = _observe(monkeypatch, db)
    assert _import(db, tmp_path) == []
    assert db.events == ["full-flush"] and creates == patches == keys == []


def test_source_hash_and_raw_limits_do_not_include_default_settings(monkeypatch, tmp_path):
    path = _lab(tmp_path)
    spec = lab_content.parse_lab(path)
    before = lab_content._source_hash(spec, "html")
    for threshold in (0.2, 0.9):
        db = ObservedSession([None])
        _observe(monkeypatch, db, _cfg() | {"learning_lab_pass_threshold": threshold})
        row = _import(db, tmp_path)[0]
        assert row.limits == {} and spec.limits == {}
        assert lab_content._source_hash(spec, "html") == before


def test_insert_failure_precedes_template_add_and_final_flush(monkeypatch, tmp_path):
    _lab(tmp_path, threshold="0.4")
    db = ObservedSession([None])
    _observe(monkeypatch, db)

    def refuse(session, payload):
        db.events.append("owner-create-failure")
        raise RuntimeError("Activity INSERT")

    monkeypatch.setattr(lab_content.learning, "create_activity_identity", refuse)
    with pytest.raises(RuntimeError, match="Activity INSERT"):
        _import(db, tmp_path)
    assert db.events[-1] == "owner-create-failure"
    assert "template-add" not in db.events and "full-flush" not in db.events


def test_final_template_flush_failure_leaves_staged_template_in_caller_session(monkeypatch, tmp_path):
    _lab(tmp_path)
    db = ObservedSession([None])
    _observe(monkeypatch, db)
    original = db.flush

    def flush():
        original()
        if db.added:
            raise RuntimeError("Template flush")

    db.flush = flush
    with pytest.raises(RuntimeError, match="Template flush"):
        _import(db, tmp_path)
    assert len(db.added) == 1
    assert db.events[-2:] == ["template-add", "full-flush"]


def test_later_parse_failure_leaves_first_template_staged_without_final_flush(monkeypatch, tmp_path):
    _lab(tmp_path)
    broken = _lab(tmp_path, "b")
    (broken / "topo.clab.yml").unlink()
    db = ObservedSession([None])
    _observe(monkeypatch, db)
    with pytest.raises(FileNotFoundError):
        _import(db, tmp_path)
    assert len(db.added) == 1
    assert db.events[-2:] == ["full-flush", "template-add"]
    assert db.events.count("settings-query") == 1


def test_missing_default_null_fails_before_template_query(monkeypatch, tmp_path):
    _lab(tmp_path)
    db = ObservedSession()
    _, creates, patches, keys = _observe(monkeypatch, db, _cfg() | {"learning_lab_pass_threshold": None})
    with pytest.raises(TypeError):
        _import(db, tmp_path)
    assert db.events == ["settings-enter", "settings-query", "settings-exit"]
    assert creates == patches == [] and keys == ["learning_lab_pass_threshold"]


def test_empty_slug_has_no_settings_or_template_read_but_final_flush(monkeypatch, tmp_path):
    path = _lab(tmp_path)
    (path / "lab.yaml").write_text("title: No slug\n")
    db = ObservedSession()
    _, creates, patches, keys = _observe(monkeypatch, db)
    assert _import(db, tmp_path) == []
    assert db.events == ["full-flush"] and creates == patches == keys == []
