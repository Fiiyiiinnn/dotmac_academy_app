"""TDD tests for the Foundation content import service."""

import hashlib
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import dotmac_learning as learning
import pytest

from app.services import content_import
from app.services.content_import import (
    import_foundation,
    import_manual,
    missing_figure_refs,
    parse_chapter_file,
)

FX = Path(__file__).resolve().parent.parent / "fixtures" / "manual"


def test_parse_chapter_strips_frontmatter_and_renders():
    doc = parse_chapter_file(FX / "chapters" / "chapter-01.md", FX / "figures" / "final")
    assert doc.number == 1 and doc.title == "Welcome" and doc.part == "I"
    assert "<h1>" in doc.body_html and "manual: foundation" not in doc.body_html
    assert "placeholder" in doc.body_html  # missing figure → placeholder


def test_import_is_idempotent(admin_session, tenant_a):
    c1 = import_foundation(
        admin_session,
        tenant_id=tenant_a.id,
        chapters_dir=FX / "chapters",
        figures_dir=FX / "figures" / "final",
        strict_figures=False,
    )
    admin_session.flush()
    n1 = len(learning.authoring_chapters(admin_session, tenant_id=tenant_a.id, course_id=c1.id))
    c2 = import_foundation(
        admin_session,
        tenant_id=tenant_a.id,
        chapters_dir=FX / "chapters",
        figures_dir=FX / "figures" / "final",
        strict_figures=False,
    )
    admin_session.flush()
    n2 = len(learning.authoring_chapters(admin_session, tenant_id=tenant_a.id, course_id=c2.id))
    assert c1.id == c2.id and n1 == n2 == 1
    assert c2.title == "Network Foundation"
    admin_session.rollback()


def test_import_manual_custom_slug_distinct_from_foundation(admin_session, tenant_a):
    found = import_foundation(
        admin_session,
        tenant_id=tenant_a.id,
        chapters_dir=FX / "chapters",
        figures_dir=FX / "figures" / "final",
        strict_figures=False,
    )
    fiber = import_manual(
        admin_session,
        tenant_id=tenant_a.id,
        slug="fiber-engineering",
        title="Technical Academy — Fiber Engineering",
        discipline="fiber",
        source_ref="fiber-engineering@0.1.0",
        chapters_dir=FX / "chapters",
        figures_dir=FX / "figures" / "final",
        strict_figures=False,
    )
    admin_session.flush()
    # Distinct course with the requested identity, imported alongside Foundation.
    assert fiber.id != found.id
    assert fiber.slug == "fiber-engineering"
    assert fiber.title == "Fiber Engineering"
    assert fiber.discipline == "fiber"
    assert len(learning.all_courses(admin_session, tenant_id=tenant_a.id)) == 2
    # Idempotent re-import returns the same course.
    again = import_manual(
        admin_session,
        tenant_id=tenant_a.id,
        slug="fiber-engineering",
        title="Fiber Engineering",
        discipline="fiber",
        source_ref="fiber-engineering@0.1.0",
        chapters_dir=FX / "chapters",
        figures_dir=FX / "figures" / "final",
        strict_figures=False,
    )
    assert again.id == fiber.id
    admin_session.rollback()


def test_import_rejects_missing_figures_by_default():
    missing = missing_figure_refs(FX / "chapters", FX / "figures" / "final")
    assert missing == {"chapter-01.md": ["FND-01-09"]}
    with pytest.raises(ValueError, match="FND-01-09"):
        import_foundation(
            None,
            tenant_id="tenant-id",
            chapters_dir=FX / "chapters",
            figures_dir=FX / "figures" / "final",
        )


def test_metadata_reimport_bumps_version(admin_session, tenant_a):
    course = import_manual(
        admin_session,
        tenant_id=tenant_a.id,
        slug="fiber-engineering",
        title="Fiber Engineering",
        discipline="fiber",
        source_ref="fiber@0.1.0",
        chapters_dir=FX / "chapters",
        figures_dir=FX / "figures" / "final",
        strict_figures=False,
    )
    admin_session.flush()
    v1 = course.version

    again = import_manual(
        admin_session,
        tenant_id=tenant_a.id,
        slug="fiber-engineering",
        title="Technical Academy — Fiber Engineering",
        discipline="networking",
        source_ref="fiber@0.2.0",
        chapters_dir=FX / "chapters",
        figures_dir=FX / "figures" / "final",
        strict_figures=False,
    )
    admin_session.flush()

    assert again.version == v1 + 1
    assert again.discipline == "networking"
    assert again.source_ref == "fiber@0.2.0"
    admin_session.rollback()


class _CallerSession:
    """Any adapter persistence or transaction operation must fail immediately."""

    def __getattr__(self, name):
        pytest.fail(f"adapter touched caller Session.{name}")


def _write_chapter(directory, filename="chapter-01.md", *, number=1, title="Welcome", body="# Welcome\n"):
    directory.mkdir(exist_ok=True)
    path = directory / filename
    path.write_text(
        f"---\nchapter: {number}\ntitle: {title}\npart: I\n---\n{body}",
        encoding="utf-8",
    )
    return path


def _manual(db, tenant_id, chapters_dir, figures_dir, **overrides):
    options = dict(
        tenant_id=tenant_id,
        slug="Manual Slug",
        title="Technical Academy — Manual",
        discipline="networking",
        source_ref="manual@1",
        chapters_dir=chapters_dir,
        figures_dir=figures_dir,
    )
    options.update(overrides)
    return import_manual(db, **options)


def test_import_public_command_same_session_tenant_sorted_sources_and_return(monkeypatch, tmp_path):
    chapters = tmp_path / "chapters"
    _write_chapter(chapters, "chapter-02.md", number=1, title="Second encounter")
    _write_chapter(chapters, "chapter-01.md", number=9, title="First encounter")
    _write_chapter(chapters, "ignored.md", number=2)
    figures = tmp_path / "figures"
    db, tenant_id = _CallerSession(), uuid4()
    result = SimpleNamespace(id=uuid4(), slug="Manual Slug", version=2)
    calls = []

    def owner(session, command, *, chapters):
        assert session is db
        assert type(command) is learning.ManualImportHeader
        assert command.tenant_id == tenant_id
        assert (command.slug, command.title, command.discipline, command.source_ref) == (
            "Manual Slug",
            "Manual",
            "networking",
            "manual@1",
        )
        # Creation defaults ride the header; Learning writes them only when
        # the course row is absent (existing policy is never restated).
        assert (
            command.new_course_completion_fraction,
            command.new_course_certificate_auto_issue,
            command.new_course_certificate_validity_days,
            command.new_course_prerequisite_composition,
        ) == (1.0, True, None, "all")
        chapters = tuple(chapters)
        assert [(doc.number, doc.title) for doc in chapters] == [(9, "First encounter"), (1, "Second encounter")]
        for doc in chapters:
            assert type(doc) is learning.ImportedChapter
            assert "body_md" not in doc.model_fields
            assert "order_index" not in doc.model_fields
            assert doc.part == "I"
            assert "<h1>Welcome</h1>" in doc.body_html
            filename = "chapter-01.md" if doc.number == 9 else "chapter-02.md"
            raw = (tmp_path / "chapters" / filename).read_text(encoding="utf-8")
            assert doc.source_hash == hashlib.sha256(raw.encode() + hashlib.sha256().hexdigest().encode()).hexdigest()
        calls.append(command)
        return result

    monkeypatch.setattr(learning, "import_manual_chapters", owner)
    assert _manual(db, tenant_id, chapters, figures) is result
    assert len(calls) == 1


def test_import_keeps_duplicate_number_encounter_order(monkeypatch, tmp_path):
    chapters = tmp_path / "chapters"
    _write_chapter(chapters, "chapter-02.md", number=1, title="Second")
    _write_chapter(chapters, "chapter-01.md", number=1, title="First")

    def owner(db, command, *, chapters):
        assert [(doc.number, doc.title) for doc in chapters] == [(1, "First"), (1, "Second")]

    monkeypatch.setattr(learning, "import_manual_chapters", owner)
    _manual(_CallerSession(), uuid4(), chapters, tmp_path / "figures")


def test_parse_public_source_hash_exact_text_distinct_figures(monkeypatch, tmp_path):
    chapters, figures = tmp_path / "chapters", tmp_path / "figures"
    figures.mkdir()
    (figures / "B-02.png").write_bytes(b"figure bytes\x00")
    path = _write_chapter(chapters, body="# Héllo\nFIGURE `B-02` *Caption*\nFIGURE `A-01`\nFIGURE `B-02`\n")
    raw = path.read_text(encoding="utf-8")
    original = learning.chapter_source_hash
    seen = []

    def source_hash(source):
        assert type(source) is learning.ChapterSource
        assert source.raw_utf8 == raw.encode()
        assert [(item.id, item.content) for item in source.figures] == [("A-01", None), ("B-02", b"figure bytes\x00")]
        seen.append(source)
        return original(source)

    monkeypatch.setattr(learning, "chapter_source_hash", source_hash)
    doc = parse_chapter_file(path, figures)
    fingerprint = hashlib.sha256(b"A-01<missing>B-02figure bytes\x00").hexdigest()
    assert doc.source_hash == hashlib.sha256(raw.encode() + fingerprint.encode()).hexdigest()
    assert len(seen) == 1


def test_source_hash_changes_with_figures_and_raw_not_renderer(monkeypatch, tmp_path):
    chapters, figures = tmp_path / "chapters", tmp_path / "figures"
    figures.mkdir()
    path = _write_chapter(chapters, body="# Welcome\nFIGURE `A-01`\n")
    first = parse_chapter_file(path, figures)
    monkeypatch.setattr(content_import, "render_markdown", lambda body: "different renderer")
    assert parse_chapter_file(path, figures).source_hash == first.source_hash
    (figures / "A-01.png").write_bytes(b"new")
    produced = parse_chapter_file(path, figures)
    assert produced.source_hash != first.source_hash
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert parse_chapter_file(path, figures).source_hash != produced.source_hash


def test_missing_figures_precede_bad_yaml_tenant_and_owner(monkeypatch, tmp_path):
    chapters = tmp_path / "chapters"
    chapters.mkdir()
    (chapters / "chapter-01.md").write_text("---\nbroken: [\n---\nFIGURE `Z-01`\n", encoding="utf-8")
    monkeypatch.setattr(
        learning, "import_manual_chapters", lambda *args, **kwargs: pytest.fail("owner called before figures")
    )
    monkeypatch.setattr(content_import, "parse_chapter_file", lambda *args: pytest.fail("parsed before figures"))
    with pytest.raises(ValueError, match="missing figure assets for import: chapter-01.md: Z-01"):
        _manual(_CallerSession(), "invalid tenant", chapters, tmp_path / "figures")


def test_owner_exception_propagates_unchanged(monkeypatch, tmp_path):
    error = RuntimeError("owner failure")

    def owner(db, command, *, chapters):
        raise error

    monkeypatch.setattr(learning, "import_manual_chapters", owner)
    with pytest.raises(RuntimeError) as raised:
        _manual(_CallerSession(), uuid4(), tmp_path / "missing", tmp_path / "figures")
    assert raised.value is error


def test_empty_import_still_calls_owner_with_no_chapters(monkeypatch, tmp_path):
    seen = []

    def owner(db, command, *, chapters):
        assert tuple(chapters) == ()
        seen.append(command)

    monkeypatch.setattr(learning, "import_manual_chapters", owner)
    _manual(_CallerSession(), uuid4(), tmp_path / "missing", tmp_path / "figures")
    assert len(seen) == 1


def test_file_discovery_and_each_parse_are_lazy_after_owner_entry(monkeypatch, tmp_path):
    chapters_dir = tmp_path / "chapters"
    _write_chapter(chapters_dir, "chapter-01.md")
    _write_chapter(chapters_dir, "chapter-02.md", number=2)
    trace = []
    glob = Path.glob
    parse = content_import.parse_chapter_file

    def observe_glob(path, pattern):
        trace.append("discover")
        return glob(path, pattern)

    def observe_parse(path, figures):
        trace.append(path.name)
        return parse(path, figures)

    def owner(db, command, *, chapters):
        assert trace == []
        trace.append("owner staged metadata")
        assert next(chapters).number == 1
        assert trace == ["owner staged metadata", "discover", "chapter-01.md"]
        trace.append("owner staged chapter")
        assert next(chapters).number == 2
        with pytest.raises(StopIteration):
            next(chapters)

    monkeypatch.setattr(Path, "glob", observe_glob)
    monkeypatch.setattr(content_import, "parse_chapter_file", observe_parse)
    monkeypatch.setattr(learning, "import_manual_chapters", owner)
    _manual(_CallerSession(), uuid4(), chapters_dir, tmp_path / "figures", strict_figures=False)
    assert trace == ["owner staged metadata", "discover", "chapter-01.md", "owner staged chapter", "chapter-02.md"]


def test_later_parser_failure_reaches_owner_after_first_yield(monkeypatch, tmp_path):
    chapters_dir = tmp_path / "chapters"
    _write_chapter(chapters_dir, "chapter-01.md")
    _write_chapter(chapters_dir, "chapter-02.md", number=2)
    failure = RuntimeError("renderer failed on second source")
    parse = content_import.parse_chapter_file
    seen = []

    def fail_second(path, figures):
        if path.name == "chapter-02.md":
            raise failure
        return parse(path, figures)

    def owner(db, command, *, chapters):
        seen.append(next(chapters).number)
        next(chapters)
        pytest.fail("source failure suppressed")

    monkeypatch.setattr(content_import, "parse_chapter_file", fail_second)
    monkeypatch.setattr(learning, "import_manual_chapters", owner)
    with pytest.raises(RuntimeError) as raised:
        _manual(_CallerSession(), uuid4(), chapters_dir, tmp_path / "figures")
    assert raised.value is failure and seen == [1]


def test_foundation_wrapper_supplies_original_identity(monkeypatch, tmp_path):
    db, tenant_id = _CallerSession(), uuid4()
    fact = SimpleNamespace(id=uuid4(), slug="foundation", version=1)

    def owner(session, command, *, chapters):
        assert session is db and command.tenant_id == tenant_id
        assert (command.slug, command.title, command.discipline, command.source_ref) == (
            "foundation",
            "Network Foundation",
            "networking",
            "foundation@0.1.0",
        )
        assert tuple(chapters) == ()
        return fact

    monkeypatch.setattr(learning, "import_manual_chapters", owner)
    assert (
        import_foundation(db, tenant_id=tenant_id, chapters_dir=tmp_path / "missing", figures_dir=tmp_path / "figures")
        is fact
    )


def test_cli_manual_value_caller_commits_then_syncs_same_result(monkeypatch, tmp_path, capsys):
    from app import cli

    tenant_id = uuid4()
    trace = []

    class Caller:
        def commit(self):
            trace.append("commit")

    db = Caller()

    @contextmanager
    def tenant_session(slug):
        assert slug == "tenant"
        yield db, SimpleNamespace(id=tenant_id)

    def owner(session, command, *, chapters):
        assert session is db and command.tenant_id == tenant_id
        assert command.source_ref == "manual@0.1.0"
        assert tuple(chapters) == ()
        trace.append("owner")
        return SimpleNamespace(id="course-id", slug="manual", version=1)

    def sync(figures_dir, static_figures):
        assert trace == ["owner", "commit"]
        assert figures_dir == tmp_path / "figures" and static_figures.name == "figures"
        trace.append("sync")
        return 3

    monkeypatch.setattr(cli, "_tenant_session", tenant_session)
    monkeypatch.setattr(learning, "import_manual_chapters", owner)
    monkeypatch.setattr(content_import, "sync_figures", sync)
    cli._import_manual(
        SimpleNamespace(
            tenant_slug="tenant",
            slug="manual",
            title="Manual",
            discipline="x",
            source_ref=None,
            chapters_dir=tmp_path / "missing",
            figures_dir=tmp_path / "figures",
            allow_missing_figures=False,
        )
    )
    assert trace == ["owner", "commit", "sync"]
    assert capsys.readouterr().out == (
        "course 'manual' (course-id) v1 imported; 3 figure(s) synced to static/figures/\n"
    )


@pytest.fixture(autouse=True)
def _explicit_learning_policy_settings(monkeypatch):
    # Characterization data belongs to the product fixture, never the library.
    from app.services import learning_policy, settings_store

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
                "learning_prerequisite_default_kind": "course_completed",
                "learning_prerequisite_default_composition": "all",
                "learning_track_default_gating_mode": "previous",
                "learning_default_completion_fraction": 1.0,
                "learning_default_certificate_auto_issue": True,
                "learning_default_certificate_validity_days": 0,
                "learning_default_activity_scoring_policy": "best",
                "learning_default_attempt_cooldown_seconds": 0,
                "learning_session_promotes_delivery_mode": True,
                "learning_pacing_default_open": True,
            }
        return original(db)

    monkeypatch.setattr(settings_store, "effective", effective)
    monkeypatch.setattr(learning_policy, "effective", effective)
