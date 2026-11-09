"""Foundation manual content import service.

Parses markdown chapter files (with YAML frontmatter) into rendered HTML and
delegates curriculum persistence to the public Learning owner.
"""

# Ported at new-composition genesis (learning-facing wave 2).

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import dotmac_learning as learning
import yaml
from sqlalchemy.orm import Session

from app.services import settings_store
from app.services.authoring import render_markdown
from app.services.learning_policy import creation_defaults


def sync_figures(figures_dir: Path, static_figures_dir: Path) -> int:
    """Copy produced figure PNGs into the app's static/figures/ directory.

    Chapter HTML references figures at ``/static/figures/<id>.png``; produced
    figures live in the source ``figures_dir`` and must be copied into the
    served ``static/`` tree or they 404 in the browser. Returns the number of
    files copied. Safe to call repeatedly (overwrites).
    """
    figures_dir = Path(figures_dir)
    static_figures_dir = Path(static_figures_dir)
    if not figures_dir.is_dir():
        return 0
    static_figures_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    for png in figures_dir.glob("*.png"):
        shutil.copy2(png, static_figures_dir / png.name)
        copied += 1
    return copied


# Matches the YAML frontmatter block at the top of a file.
_FM = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)

# Matches FIGURE callouts like: FIGURE `FND-01-09` *optional caption*
_FIG = re.compile(r"FIGURE `([A-Z0-9\-]+)`(?:\s*\*(.*?)\*)?")


def _course_display_title(title: str) -> str:
    """Return the learner-facing catalog title for an imported manual."""
    return title.removeprefix("Technical Academy — ").strip()


def missing_figure_refs(chapters_dir: Path, figures_dir: Path) -> dict[str, list[str]]:
    """Return missing figure ids by chapter filename."""
    figures_dir = Path(figures_dir)
    missing: dict[str, list[str]] = {}
    for chapter_file in sorted(Path(chapters_dir).glob("chapter-*.md")):
        raw = chapter_file.read_text(encoding="utf-8")
        refs = sorted({m.group(1) for m in _FIG.finditer(raw)})
        absent = [fid for fid in refs if not (figures_dir / f"{fid}.png").exists()]
        if absent:
            missing[chapter_file.name] = absent
    return missing


def _chapter_source(raw: str, figures_dir: Path) -> learning.ChapterSource:
    """Supply exact raw text and distinct referenced figure bytes to the owner."""
    figures = []
    for fid in sorted({m.group(1) for m in _FIG.finditer(raw)}):
        img_path = Path(figures_dir) / f"{fid}.png"
        figures.append(learning.FigureSource(id=fid, content=img_path.read_bytes() if img_path.exists() else None))
    return learning.ChapterSource(raw_utf8=raw.encode(), figures=tuple(figures))


@dataclass
class ChapterDoc:
    """Parsed representation of a chapter markdown file."""

    number: int
    title: str
    part: str
    body_html: str
    source_hash: str


def _figure_sub(figures_dir: Path):
    """Return a regex replacement function that substitutes FIGURE callouts."""

    def repl(m: re.Match) -> str:
        fid = m.group(1)
        caption = m.group(2) or ""
        img_path = figures_dir / f"{fid}.png"
        if img_path.exists():
            return (
                f"<figure>"
                f'<img src="/static/figures/{fid}.png" alt="{caption}">'
                f"<figcaption>{caption}</figcaption>"
                f"</figure>"
            )
        return (
            f'<figure class="placeholder"><figcaption>[figure {fid} not yet produced] {caption}</figcaption></figure>'
        )

    return repl


def parse_chapter_file(path: Path, figures_dir: Path) -> ChapterDoc:
    """Parse a markdown chapter file into a ChapterDoc.

    Strips YAML frontmatter, substitutes FIGURE callouts, then renders the
    remaining markdown to HTML.

    Args:
        path: Path to the ``chapter-NN.md`` file.
        figures_dir: Directory to search for produced figure PNG files.

    Returns:
        A :class:`ChapterDoc` with rendered HTML and metadata from frontmatter.
    """
    raw = Path(path).read_text(encoding="utf-8")
    source_hash = learning.chapter_source_hash(_chapter_source(raw, figures_dir))

    fm_match = _FM.match(raw)
    meta: dict = yaml.safe_load(fm_match.group(1)) if fm_match else {}
    body_md = raw[fm_match.end() :] if fm_match else raw

    # Substitute FIGURE callouts before markdown rendering so they become raw
    # HTML blocks (markdown won't re-process them).
    body_md = _FIG.sub(_figure_sub(figures_dir), body_md)

    body_html = render_markdown(body_md)

    return ChapterDoc(
        number=int(meta.get("chapter", 0)),
        title=str(meta.get("title", "")),
        part=str(meta.get("part", "")),
        body_html=body_html,
        source_hash=source_hash,
    )


def import_manual(
    db: Session,
    *,
    tenant_id,
    slug: str,
    title: str,
    discipline: str,
    source_ref: str,
    chapters_dir: Path,
    figures_dir: Path,
    strict_figures: bool = True,
) -> learning.CourseFact:
    """Upsert a course and its chapters from a directory of markdown files.

    Generic over any manual. Idempotent: chapters whose ``source_hash`` has not
    changed are skipped. ``Course.version`` is bumped only when at least one
    chapter changed or was added.

    Args:
        db: Caller-owned SQLAlchemy session with an active tenant transaction.
        tenant_id: UUID of the tenant to import into.
        slug: URL-safe course identifier (e.g. ``"foundation"``, ``"fiber-engineering"``).
        title: Human-readable course title.
        discipline: Course discipline tag (e.g. ``"networking"``, ``"fiber"``).
        source_ref: Provenance recorded on the course (e.g. ``"fiber-engineering@0.1.0"``).
        chapters_dir: Directory containing ``chapter-*.md`` files.
        figures_dir: Directory containing produced figure PNG files.
        strict_figures: When true, abort if any chapter references a missing figure.

    Returns:
        An immutable course fact with the imported course values.
    """
    if strict_figures:
        missing = missing_figure_refs(chapters_dir, figures_dir)
        if missing:
            details = "; ".join(f"{chapter}: {', '.join(ids)}" for chapter, ids in missing.items())
            raise ValueError(f"missing figure assets for import: {details}")

    defaults = creation_defaults(db)
    command = learning.ManualImportHeader(
        tenant_id=tenant_id,
        slug=slug,
        title=_course_display_title(title),
        discipline=discipline,
        source_ref=source_ref,
        new_course_status=cast(str, settings_store.effective(db, require_store=True)["learning_default_course_status"]),
        new_course_listed=cast(
            bool, settings_store.effective(db, require_store=True)["learning_default_course_listed"]
        ),
        new_course_completion_fraction=defaults.completion_fraction,
        new_course_certificate_auto_issue=defaults.certificate_auto_issue,
        new_course_certificate_validity_days=defaults.certificate_validity_days,
        new_course_prerequisite_composition=defaults.prerequisite_default_composition,
    )

    def chapters():
        # Discovery and parsing occur after the owner stages course metadata.
        for chapter_file in sorted(Path(chapters_dir).glob("chapter-*.md")):
            doc = parse_chapter_file(chapter_file, figures_dir)
            yield learning.ImportedChapter(
                number=doc.number,
                title=doc.title,
                part=doc.part,
                body_html=doc.body_html,
                source_hash=doc.source_hash,
            )

    return learning.import_manual_chapters(db, command, chapters=chapters())


def import_foundation(
    db: Session,
    *,
    tenant_id,
    chapters_dir: Path,
    figures_dir: Path,
    strict_figures: bool = True,
) -> learning.CourseFact:
    """Backward-compatible wrapper: import the Foundation manual.

    Thin convenience over :func:`import_manual` with the Foundation course
    identity. Kept so existing callers/tests stay unchanged.
    """
    return import_manual(
        db,
        tenant_id=tenant_id,
        slug="foundation",
        title="Network Foundation",
        discipline="networking",
        source_ref="foundation@0.1.0",
        chapters_dir=chapters_dir,
        figures_dir=figures_dir,
        strict_figures=strict_figures,
    )
