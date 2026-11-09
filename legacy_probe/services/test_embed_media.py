"""Tests for embed_media and the XSS-safe render_markdown pipeline (Feature 7).

Pure-function tests — no database required for the embed / sanitisation logic.
The end-to-end render_markdown tests confirm that the full pipeline (markdown →
sanitise → embed) is secure against common XSS vectors.
"""

from __future__ import annotations

import pytest

from app.services.authoring import embed_media, render_markdown

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _a(href: str, text: str = "link") -> str:
    """Produce a sanitised <a> tag exactly as embed_media would receive it."""
    return f'<a href="{href}">{text}</a>'


# ---------------------------------------------------------------------------
# YouTube embeds
# ---------------------------------------------------------------------------


class TestYouTubeEmbeds:
    def test_youtube_watch_link_produces_iframe(self) -> None:
        result = embed_media(_a("https://www.youtube.com/watch?v=dQw4w9WgXcY"))
        assert "<iframe" in result
        assert "https://www.youtube.com/embed/dQw4w9WgXcY" in result
        assert result.count("<iframe") == 1

    def test_youtu_be_link_produces_iframe(self) -> None:
        result = embed_media(_a("https://youtu.be/dQw4w9WgXcY"))
        assert "<iframe" in result
        assert "https://www.youtube.com/embed/dQw4w9WgXcY" in result

    def test_youtube_embed_uses_only_validated_id(self) -> None:
        # Extra query params present — only the ID must enter the embed src.
        result = embed_media(_a("https://www.youtube.com/watch?v=abc123&amp;t=30"))
        assert "https://www.youtube.com/embed/abc123" in result
        assert "t=30" not in result

    def test_youtube_watch_v_not_first_param(self) -> None:
        # v= appearing after another param (HTML-entity-encoded ampersand).
        result = embed_media(_a("https://www.youtube.com/watch?list=PLxxx&amp;v=abc123"))
        assert "https://www.youtube.com/embed/abc123" in result

    def test_youtube_no_script_in_embed(self) -> None:
        result = embed_media(_a("https://www.youtube.com/watch?v=dQw4w9WgXcY"))
        assert "<script" not in result.lower()

    def test_youtube_lookalike_host_not_embedded(self) -> None:
        # evil.com with a path that contains "youtube.com" — must NOT embed.
        result = embed_media(_a("https://evil.com/youtube.com/watch?v=abc123"))
        assert "<iframe" not in result
        assert 'href="https://evil.com/' in result

    def test_youtube_host_as_path_component_not_embedded(self) -> None:
        result = embed_media(_a("https://evil.com/watch?v=abc123"))
        assert "<iframe" not in result

    def test_youtube_invalid_id_special_chars_not_embedded(self) -> None:
        # After sanitisation, dangerous chars in the href are HTML-encoded.
        # The strict ID regex [A-Za-z0-9_-]+ rejects them → no iframe.
        # Use render_markdown so the string goes through the sanitiser first.
        result = render_markdown('[watch](https://www.youtube.com/watch?v="><script>x</script>)')
        assert "<iframe" not in result
        assert "<script>" not in result

    def test_youtube_no_v_param_not_embedded(self) -> None:
        result = embed_media(_a("https://www.youtube.com/watch?list=PLxxx"))
        assert "<iframe" not in result


# ---------------------------------------------------------------------------
# Vimeo embeds
# ---------------------------------------------------------------------------


class TestVimeoEmbeds:
    def test_vimeo_link_produces_iframe(self) -> None:
        result = embed_media(_a("https://vimeo.com/123456789"))
        assert "<iframe" in result
        assert "https://player.vimeo.com/video/123456789" in result
        assert result.count("<iframe") == 1

    def test_vimeo_www_prefix_accepted(self) -> None:
        result = embed_media(_a("https://www.vimeo.com/987654321"))
        assert "https://player.vimeo.com/video/987654321" in result

    def test_vimeo_non_numeric_id_not_embedded(self) -> None:
        result = embed_media(_a("https://vimeo.com/channels/staff"))
        assert "<iframe" not in result

    def test_vimeo_lookalike_not_embedded(self) -> None:
        result = embed_media(_a("https://notvimeo.com/123456789"))
        assert "<iframe" not in result

    def test_vimeo_no_script_in_embed(self) -> None:
        result = embed_media(_a("https://vimeo.com/123456789"))
        assert "<script" not in result.lower()


# ---------------------------------------------------------------------------
# Self-hosted video files
# ---------------------------------------------------------------------------


class TestVideoFileEmbeds:
    @pytest.mark.parametrize("ext", ["mp4", "webm", "ogg", "MP4", "WebM"])
    def test_video_extension_produces_video_element(self, ext: str) -> None:
        url = f"https://cdn.example.com/lecture.{ext}"
        result = embed_media(_a(url))
        assert "<video" in result
        assert "<iframe" not in result

    def test_video_src_is_exact_original_url(self) -> None:
        url = "https://cdn.example.com/lecture.mp4"
        result = embed_media(_a(url))
        assert f'src="{url}"' in result

    def test_video_has_controls_attribute(self) -> None:
        result = embed_media(_a("https://cdn.example.com/demo.webm"))
        assert "controls" in result

    def test_video_with_query_string(self) -> None:
        url = "https://cdn.example.com/demo.mp4?version=2"
        result = embed_media(_a(url))
        assert "<video" in result


# ---------------------------------------------------------------------------
# Document download links
# ---------------------------------------------------------------------------


class TestDocumentDownloadLinks:
    @pytest.mark.parametrize("ext", ["pdf", "zip", "csv", "pcap", "PDF"])
    def test_doc_extension_gets_download_class(self, ext: str) -> None:
        url = f"https://example.com/file.{ext}"
        result = embed_media(_a(url, "Download"))
        assert 'class="download-link"' in result
        assert f'href="{url}"' in result
        assert "<iframe" not in result
        assert "<video" not in result

    def test_download_link_preserves_inner_text(self) -> None:
        result = embed_media(_a("https://example.com/report.pdf", "Annual Report"))
        assert "Annual Report" in result

    def test_download_link_preserves_href(self) -> None:
        url = "https://example.com/labs.zip"
        result = embed_media(_a(url))
        assert f'href="{url}"' in result


# ---------------------------------------------------------------------------
# Pass-through cases
# ---------------------------------------------------------------------------


class TestPassThrough:
    def test_ordinary_https_link_unchanged(self) -> None:
        html = _a("https://example.com/page", "Click here")
        assert embed_media(html) == html

    def test_plain_paragraph_unchanged(self) -> None:
        text = "<p>Hello world</p>"
        assert embed_media(text) == text

    def test_anchor_without_href_unchanged(self) -> None:
        # Named anchor — no href attribute.
        html = '<a name="section-1">Anchor</a>'
        result = embed_media(html)
        assert "<iframe" not in result
        assert "<video" not in result

    def test_no_links_in_input(self) -> None:
        html = "<p>Just <strong>text</strong> here.</p>"
        assert embed_media(html) == html


# ---------------------------------------------------------------------------
# render_markdown end-to-end XSS safety
# ---------------------------------------------------------------------------


class TestRenderMarkdownXSSSafety:
    def test_script_tag_neutralized(self) -> None:
        """Raw <script> in markdown source must not appear in the output."""
        result = render_markdown("<script>alert(document.cookie)</script>\n\nHello.")
        assert "<script" not in result.lower()
        # Content of the script block is also suppressed.
        assert "alert(" not in result

    def test_on_event_attribute_stripped(self) -> None:
        """on* event handlers must be stripped from the sanitised output."""
        result = render_markdown('<img src="x" onerror="alert(1)">')
        assert "onerror" not in result

    def test_javascript_href_neutralized(self) -> None:
        """javascript: href must not survive to the output."""
        result = render_markdown("[click me](javascript:alert(1))")
        assert "javascript:" not in result

    def test_data_uri_href_neutralized(self) -> None:
        """data: URI in a link must be stripped (potentially executable)."""
        result = render_markdown("[x](data:text/html,<script>alert(1)</script>)")
        assert "data:" not in result

    def test_iframe_in_markdown_stripped(self) -> None:
        """Raw <iframe> in markdown body must be stripped (only embed_media may add iframes)."""
        result = render_markdown('<iframe src="https://evil.com/"></iframe>\n\nText.')
        assert "evil.com" not in result
        assert "<iframe" not in result

    def test_safe_markdown_still_renders(self) -> None:
        """Normal markdown (headers, bold, table) survives sanitisation."""
        source = "# Title\n\n**bold** and *italic*\n\n| a | b |\n|---|---|\n| 1 | 2 |"
        result = render_markdown(source)
        assert "<h1>" in result
        assert "<strong>" in result
        assert "<table>" in result

    def test_fenced_code_block_survives(self) -> None:
        """Code blocks with HTML-like content render safely."""
        source = "```html\n<script>alert(1)</script>\n```"
        result = render_markdown(source)
        # Code shown as escaped text, not executed.
        assert "<script" not in result
        assert "&lt;script" in result

    def test_youtube_link_in_markdown_produces_iframe(self) -> None:
        """A YouTube link authored in markdown renders to a whitelisted iframe."""
        result = render_markdown("[Watch](https://www.youtube.com/watch?v=dQw4w9WgXcY)")
        assert "<iframe" in result
        assert "youtube.com/embed/dQw4w9WgXcY" in result
        assert "<script" not in result

    def test_mp4_link_in_markdown_produces_video(self) -> None:
        result = render_markdown("[Lecture](https://cdn.example.com/lecture.mp4)")
        assert "<video" in result
        assert "lecture.mp4" in result
