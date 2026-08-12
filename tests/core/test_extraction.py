"""Real tests for the unified content-extraction pipeline (no mocks)."""

import os
import sys
from pathlib import Path

from app.crawl.extraction import (  # noqa: E402
    extract_content,
    ExtractedContent,
    _is_html,
    _looks_like_document,
    _text_from_html,
)
from app.crawl.firecrawl_format import _extract_firecrawl_metadata  # noqa: E402


class TestExtractContent:
    def test_html_uses_trafilatura_markdown(self) -> None:
        html = "<html><body><main>Main content here</main></body></html>"
        result = extract_content(html, "text/html", "http://example.com/")
        assert isinstance(result, ExtractedContent)
        assert "Main content here" in (result.markdown or "")
        assert result.text == result.markdown

    def test_html_with_charset_still_routes_to_html(self) -> None:
        body = " ".join(f"word-{i}" for i in range(300))
        html = f"<html><body><p>{body}</p></body></html>"
        result = extract_content(html, "text/html; charset=utf-8", "http://x/?q=1")
        assert len(result.markdown.split()) > 100

    def test_pdf_routes_to_markitdown_fallback(self) -> None:
        result = extract_content(b"%PDF-1.7 fake bytes", "application/pdf", "http://x/a.pdf")
        assert result.text == "%PDF-1.7 fake bytes"

    def test_csv_routes_to_markitdown_table(self) -> None:
        body = b"name,age\nAlice,30\nBob,25"
        result = extract_content(body, "text/csv", "http://x/data.csv")
        assert "Alice" in (result.text or "")
        assert "|" in (result.markdown or "")

    def test_json_passes_through_markitdown(self) -> None:
        body = b'{"a":1,"b":[2,3]}'
        result = extract_content(body, "application/json", "http://x/d.json")
        assert '"a":1' in (result.text or "")

    def test_plain_text_passes_through(self) -> None:
        result = extract_content("just some plain text", "text/plain", "http://x/note.txt")
        assert result.text == "just some plain text"
        assert result.markdown == "just some plain text"

    def test_doctype_without_content_type_routes_to_html(self) -> None:
        html = "<!doctype html><html><body>x</body></html>"
        result = extract_content(html, None, "http://x/")
        assert "x" in (result.markdown or "")

    def test_unrecognized_binary_passes_through_as_text(self) -> None:
        result = extract_content(b"\x00\x01binary", "application/octet-stream", "http://x/bin")
        assert result.text is not None

    def test_empty_or_unparseable_document_yields_no_content(self) -> None:
        result = extract_content(b"", "application/pdf", "http://x/e.pdf")
        assert result.text is None
        assert result.markdown is None


class TestRoutingHelpers:
    def test_is_html(self) -> None:
        assert _is_html("text/html", "<html></html>") is True
        assert _is_html("text/html; charset=utf-8", b"<html></html>") is True
        assert _is_html("application/json", '{"a":1}') is False
        assert _is_html(None, "<!doctype html><html></html>") is True
        assert _is_html(None, "plain") is False

    def test_looks_like_document(self) -> None:
        assert _looks_like_document("application/pdf", b"%PDF", "http://x/a.pdf") is True
        assert _looks_like_document("application/octet-stream", b"%PDF-1.4 data", "http://x/mystery") is True
        assert _looks_like_document("text/csv", b"a,b", "http://x/d.csv") is True
        assert _looks_like_document("application/json", b"{}", None) is True
        assert _looks_like_document("text/html", b"<html>", "http://x/") is False
        assert _looks_like_document("text/plain", b"hi", "http://x/note.txt") is False


class TestTextAndMetadata:
    def test_text_from_html_strips_scripts_and_tags(self) -> None:
        html = (
            "<html><body><p>Hello <b>World</b></p>"
            "<script>ignore_me();</script><style>.x{color:red}</style>"
            "</body></html>"
        )
        assert _text_from_html(html) == "Hello World"

    def test_extract_firecrawl_metadata_handles_empty(self) -> None:
        assert _extract_firecrawl_metadata("", "http://x/") == {"source_url": "http://x/"}

    def test_extract_firecrawl_metadata_parses_tags(self) -> None:
        html = (
            "<html lang='en'><head><title>Page</title>"
            "<meta name='description' content='A test page'>"
            "<meta property='og:title' content='OG'>"
            "<link rel='canonical' href='http://x/c'>"
            "<link rel='icon' href='http://x/f.ico'>"
            "</head><body><p>Body</p></body></html>"
        )
        meta = _extract_firecrawl_metadata(html, "http://x/")
        assert meta["title"] == "Page"
        assert meta["description"] == "A test page"
        assert meta["og_title"] == "OG"
        assert meta["language"] == "en"
        assert meta["canonical_url"] == "http://x/c"
        assert meta["favicon"] == "http://x/f.ico"
