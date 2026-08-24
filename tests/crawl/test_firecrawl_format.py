"""Firecrawl-format metadata extraction, including JSON-LD (SOTA)."""

from app.crawl.firecrawl_format import _extract_firecrawl_metadata, _text_from_html
from helpers import FIRECRAWL_TEST_HTML


class TestFirecrawlMetadata:
    def test_extracts_title_and_meta(self):
        meta = _extract_firecrawl_metadata(FIRECRAWL_TEST_HTML, "https://example.com")
        assert meta["title"] == "Test Page"
        assert meta["description"] == "A test page"
        assert meta["og_title"] == "OG Title"
        assert meta["language"] == "en"
        assert meta["source_url"] == "https://example.com"

    def test_handles_empty(self):
        assert _extract_firecrawl_metadata("", "https://example.com") == {"source_url": "https://example.com"}

    def test_text_from_html_strips_scripts_and_tags(self):
        html = (
            "<html><body><p>Hello <b>World</b></p>"
            "<script>ignore_me();</script><style>.x{color:red}</style>"
            "</body></html>"
        )
        assert _text_from_html(html) == "Hello World"


class TestJsonLdExtraction:
    def test_json_ld_populated_from_script_tag(self):
        html = (
            "<html><head>"
            '<script type="application/ld+json">{"@context":"https://schema.org","name":"Test"}</script>'
            "</head><body><p>Body</p></body></html>"
        )
        meta = _extract_firecrawl_metadata(html, "https://example.com")
        assert meta.get("json_ld") is not None
        assert meta["json_ld"]["name"] == "Test"
        assert meta["json_ld"]["@context"] == "https://schema.org"

    def test_json_ld_multiple_blocks_become_list(self):
        html = (
            '<script type="application/ld+json">{"@type":"A"}</script>'
            '<script type="application/ld+json">{"@type":"B"}</script>'
        )
        meta = _extract_firecrawl_metadata(html, "https://example.com")
        assert isinstance(meta["json_ld"], list)
        assert len(meta["json_ld"]) == 2

    def test_json_ld_absent_when_no_script(self):
        meta = _extract_firecrawl_metadata("<html><body>x</body></html>", "https://example.com")
        assert "json_ld" not in meta
