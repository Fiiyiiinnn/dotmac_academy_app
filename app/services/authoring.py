# app/services/authoring.py
"""In-app course authoring (Slice 5c, finding #8).

Lets instructors create draft courses and author chapters in markdown without a
filesystem import. Markdown is rendered to HTML on save (the same renderer the
import pipeline uses); the markdown source is retained in ``Chapter.body_md`` so
it stays editable. Each chapter save bumps ``Course.version``.

Rendering pipeline (``render_markdown``):
  1. ``markdown`` → raw HTML (tables + fenced_code extensions).
  2. ``_sanitize_html`` → strips script/style/iframe tags, ``on*`` event-handler
     attrs and dangerous URI schemes (javascript:, data:, vbscript:) using a
     stdlib HTMLParser whitelist.  Only the safe subset of tags/attrs survives.
  3. ``embed_media`` → replaces matching ``<a href>`` anchors with ``<video>``,
     whitelisted ``<iframe>`` (YouTube / Vimeo only; ID validated by strict
     regex), or styled download links.  Runs on *already-sanitised* HTML so it
     can only ADD a known-safe set of new elements.
"""

from __future__ import annotations

import re
from html import escape as _html_escape
from html.parser import HTMLParser
from typing import ClassVar, Protocol, cast
from uuid import UUID

import dotmac_learning as learning
import markdown as md
from sqlalchemy.orm import Session

from app.services import settings_store
from app.services.exceptions import ConflictError, NotFoundError
from app.services.learning_policy import creation_defaults

_MD_EXTENSIONS = ["tables", "fenced_code"]

# ---------------------------------------------------------------------------
# HTML sanitisation — strip unsafe tags, event-handler attrs, bad URL schemes
# ---------------------------------------------------------------------------

_SAFE_TAGS: frozenset[str] = frozenset(
    {
        "p",
        "div",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "ul",
        "ol",
        "li",
        "blockquote",
        "pre",
        "code",
        "hr",
        "br",
        "em",
        "strong",
        "a",
        "img",
        "span",
        "del",
        "ins",
        "sub",
        "sup",
        "table",
        "thead",
        "tbody",
        "tfoot",
        "tr",
        "th",
        "td",
        "caption",
        "figure",
        "figcaption",
    }
)

_SAFE_ATTRS: frozenset[str] = frozenset(
    {
        "class",
        "id",
        "href",
        "src",
        "alt",
        "title",
        "target",
        "rel",
        "colspan",
        "rowspan",
        "align",
        "valign",
        "width",
        "height",
        "start",
        "type",
    }
)

_VOID_TAGS: frozenset[str] = frozenset({"br", "hr", "img"})
_URL_ATTRS: frozenset[str] = frozenset({"href", "src"})

# Tags whose start/end *and content* are entirely suppressed.
_SUPPRESS_TAGS: frozenset[str] = frozenset({"script", "style", "noscript", "iframe", "object", "embed"})

_UNSAFE_SCHEME_RE: re.Pattern[str] = re.compile(r"(?:javascript|vbscript|data):", re.IGNORECASE)


def _safe_url(url: str) -> str:
    """Return *url* unchanged unless it carries a dangerous URI scheme."""
    # Collapse whitespace/control chars before the scheme check so that tricks
    # like " javascript:…" or "java\x00script:…" are caught.
    normalized = re.sub(r"[\x00-\x20\x7f-\x9f]+", "", url)
    return "#" if _UNSAFE_SCHEME_RE.match(normalized) else url


class _Sanitizer(HTMLParser):
    """Walk rendered HTML and re-emit only the safe subset."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._suppress_depth: int = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SUPPRESS_TAGS:
            self._suppress_depth += 1
            return
        if self._suppress_depth > 0 or tag not in _SAFE_TAGS:
            return
        parts: list[str] = [f"<{tag}"]
        for name, value in attrs:
            n = name.lower()
            if n.startswith("on") or n not in _SAFE_ATTRS:
                continue
            v = "" if value is None else value
            if n in _URL_ATTRS:
                v = _safe_url(v)
            parts.append(f' {n}="{_html_escape(v, quote=True)}"')
        parts.append(">")
        self._out.append("".join(parts))

    def handle_endtag(self, tag: str) -> None:
        if tag in _SUPPRESS_TAGS:
            if self._suppress_depth > 0:
                self._suppress_depth -= 1
            return
        if self._suppress_depth > 0 or tag not in _SAFE_TAGS or tag in _VOID_TAGS:
            return
        self._out.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self._suppress_depth == 0:
            self._out.append(_html_escape(data, quote=False))

    def handle_comment(self, data: str) -> None:
        pass  # strip HTML comments

    def handle_decl(self, decl: str) -> None:
        pass  # strip DOCTYPE declarations

    def get_output(self) -> str:
        return "".join(self._out)


def _sanitize_html(raw: str) -> str:
    """Strip all dangerous HTML from *raw*, preserving the safe subset."""
    parser = _Sanitizer()
    parser.feed(raw)
    return parser.get_output()


# ---------------------------------------------------------------------------
# Media embed transform — applied AFTER sanitisation
# ---------------------------------------------------------------------------

# Match any <a …>…</a> in the sanitised output.
_ANCHOR_RE: re.Pattern[str] = re.compile(r"<a([^>]*)>(.*?)</a>", re.IGNORECASE | re.DOTALL)
# Extract href="…" from an attribute string (sanitiser always uses double quotes).
_HREF_RE: re.Pattern[str] = re.compile(r'\bhref="([^"]*)"', re.IGNORECASE)

# YouTube watch URL — host must be exactly (www.)youtube.com
_YT_WATCH_HOST_RE: re.Pattern[str] = re.compile(r"^https?://(?:www\.)?youtube\.com/watch\?(.+)$", re.IGNORECASE)
# Extract the v= parameter (handles &amp; entity in sanitised HTML)
_YT_V_PARAM_RE: re.Pattern[str] = re.compile(r"(?:^|&amp;|&)v=([A-Za-z0-9_-]+)")
# youtu.be short links
_YT_SHORT_RE: re.Pattern[str] = re.compile(r"^https?://youtu\.be/([A-Za-z0-9_-]+)", re.IGNORECASE)
# Vimeo — numeric ID only
_VIMEO_RE: re.Pattern[str] = re.compile(r"^https?://(?:www\.)?vimeo\.com/(\d+)(?:[/?#]|$)", re.IGNORECASE)
# Self-hosted video file extensions
_VIDEO_EXT_RE: re.Pattern[str] = re.compile(r"\.(?:mp4|webm|ogg)(?:[?#]|$)", re.IGNORECASE)
# Document / downloadable file extensions
_DOC_EXT_RE: re.Pattern[str] = re.compile(r"\.(?:pdf|zip|csv|pcap)(?:[?#]|$)", re.IGNORECASE)

_IFRAME_TPL = '<div class="embed-responsive"><iframe src="{src}" frameborder="0" allowfullscreen></iframe></div>'


def _youtube_embed(href: str) -> str | None:
    """Return a responsive iframe for a YouTube URL, or *None*."""
    short = _YT_SHORT_RE.match(href)
    if short:
        vid_id = short.group(1)
        src = f"https://www.youtube.com/embed/{_html_escape(vid_id, quote=True)}"
        return _IFRAME_TPL.format(src=src)
    watch = _YT_WATCH_HOST_RE.match(href)
    if watch:
        v = _YT_V_PARAM_RE.search(watch.group(1))
        if v:
            vid_id = v.group(1)
            src = f"https://www.youtube.com/embed/{_html_escape(vid_id, quote=True)}"
            return _IFRAME_TPL.format(src=src)
    return None


def _vimeo_embed(href: str) -> str | None:
    """Return a responsive iframe for a Vimeo URL, or *None*."""
    m = _VIMEO_RE.match(href)
    if not m:
        return None
    vid_id = m.group(1)
    src = f"https://player.vimeo.com/video/{_html_escape(vid_id, quote=True)}"
    return _IFRAME_TPL.format(src=src)


def _replace_anchor(m: re.Match[str]) -> str:
    """Replacement function for ``_ANCHOR_RE.sub``."""
    attrs_str = m.group(1)
    inner = m.group(2)
    href_m = _HREF_RE.search(attrs_str)
    if not href_m:
        return m.group(0)
    href = href_m.group(1)

    yt = _youtube_embed(href)
    if yt is not None:
        return yt

    vm = _vimeo_embed(href)
    if vm is not None:
        return vm

    # Strip query string / fragment before checking file extension.
    href_path = href.split("?")[0].split("#")[0]
    if _VIDEO_EXT_RE.search(href_path):
        return f'<video controls src="{href}"></video>'
    if _DOC_EXT_RE.search(href_path):
        return f'<a href="{href}" class="download-link">{inner}</a>'

    return m.group(0)


def embed_media(body: str) -> str:
    """Replace matching ``<a href>`` anchors in sanitised HTML with rich embeds.

    This runs *after* ``_sanitize_html`` so it only adds a strictly whitelisted
    set of new elements:

    * ``<video controls src="…">`` for self-hosted ``.mp4/.webm/.ogg`` links.
    * A responsive ``<iframe>`` to ``youtube.com/embed/`` or
      ``player.vimeo.com/video/`` for recognised video-platform links.  The
      video ID is validated by a strict alphanumeric regex before being placed
      into the fixed template — arbitrary user text is never interpolated.
    * A ``class="download-link"`` ``<a>`` for ``.pdf/.zip/.csv/.pcap`` links.
    * Everything else is passed through unchanged.
    """
    return _ANCHOR_RE.sub(_replace_anchor, body)


# ---------------------------------------------------------------------------
# Public renderer
# ---------------------------------------------------------------------------


class _EditorTextParser(HTMLParser):
    """Convert existing rendered HTML into readable editor text."""

    _BLOCK_TAGS: ClassVar[set[str]] = {
        "address",
        "article",
        "aside",
        "blockquote",
        "div",
        "figcaption",
        "figure",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br":
            self.parts.append("\n")
        elif tag == "li":
            self._block_break()
            self.parts.append("- ")
        elif tag in self._BLOCK_TAGS:
            self._block_break()

    def handle_endtag(self, tag: str) -> None:
        if tag in self._BLOCK_TAGS:
            self._block_break()

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def _block_break(self) -> None:
        text = "".join(self.parts).rstrip()
        self.parts = [text, "\n\n"] if text else []

    def text(self) -> str:
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in "".join(self.parts).splitlines()]
        out: list[str] = []
        blank = False
        for line in lines:
            if line:
                out.append(line)
                blank = False
            elif not blank and out:
                out.append("")
                blank = True
        return "\n".join(out).strip()


class EditableChapter(Protocol):
    """Only the stored text fields the product editor reads."""

    @property
    def body_md(self) -> str: ...

    @property
    def body_html(self) -> str: ...


def editable_chapter_source(chapter: EditableChapter) -> str:
    """Return instructor-friendly source text for a chapter editor textarea."""
    if chapter.body_md.strip():
        return chapter.body_md
    parser = _EditorTextParser()
    parser.feed(chapter.body_html or "")
    return parser.text()


def render_markdown(body_md: str) -> str:
    """Render *body_md* to sanitised HTML and apply media embeds."""
    raw = md.markdown(body_md or "", extensions=_MD_EXTENSIONS)
    sanitized = _sanitize_html(raw)
    return embed_media(sanitized)


# ---------------------------------------------------------------------------
# Course / chapter authoring
# ---------------------------------------------------------------------------


def create_course(
    db: Session, *, tenant_id: UUID, slug: str, title: str, discipline: str, listed: bool | None = None
) -> learning.CourseFact:
    """Create a draft through the canonical owner, preserving product errors."""
    defaults = creation_defaults(db)
    try:
        return learning.create_draft_course(
            db,
            learning.CourseCreate(
                tenant_id=tenant_id,
                slug=slug,
                title=title,
                discipline=discipline,
                completion_fraction=defaults.completion_fraction,
                certificate_auto_issue=defaults.certificate_auto_issue,
                certificate_validity_days=defaults.certificate_validity_days,
                prerequisite_composition=defaults.prerequisite_default_composition,
                listed=(
                    cast(bool, settings_store.effective(db, require_store=True)["learning_default_course_listed"])
                    if listed is None
                    else listed
                ),
            ),
        )
    except learning.CurriculumConflict as exc:
        raise ConflictError(str(exc)) from None


def upsert_chapter(
    db: Session, *, tenant_id: UUID, course_id: UUID, number: int, title: str, body_md: str, part: str = ""
) -> learning.ChapterFact:
    """Delegate course lookup, product rendering, then canonical chapter write."""
    try:
        return learning.author_chapter(
            db,
            learning.AuthorChapterCommand(
                tenant_id=tenant_id,
                course_id=course_id,
                number=number,
                title=title,
                body_md=body_md,
                part=part,
            ),
            render_markdown=render_markdown,
        )
    except learning.CurriculumNotFound as exc:
        raise NotFoundError(str(exc)) from None


def delete_chapter(db: Session, *, tenant_id: UUID, course_id: UUID, chapter_id: UUID) -> None:
    """Delete through the canonical owner; required failures remain uncaught."""
    try:
        learning.delete_chapter(db, tenant_id=tenant_id, course_id=course_id, chapter_id=chapter_id)
    except learning.CurriculumNotFound as exc:
        raise NotFoundError(str(exc)) from None
