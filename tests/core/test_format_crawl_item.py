"""Unified output formatting: every crawl strategy funnels through one path."""

from app.main import _format_crawl_item  # noqa: E402


class TestFormatCrawlItemUnified:
    def test_html_result_uses_markdownify(self) -> None:
        item = {
            "final_url": "http://example.com/",
            "html": "<html><head><title>T</title></head><body><p>Body text here</p></body></html>",
        }
        markdown, metadata = _format_crawl_item(item)
        assert markdown is not None
        assert "Body text here" in markdown
        assert metadata.get("title") == "T"
        assert metadata.get("source_url") == "http://example.com/"

    def test_text_only_result_falls_back_to_extracted_text(self) -> None:
        item = {
            "final_url": "http://example.com/doc.pdf",
            "text": "extracted pdf text",
        }
        markdown, metadata = _format_crawl_item(item)
        assert markdown == "extracted pdf text"
        # No HTML to parse, so only the basic metadata survives.
        assert metadata == {}

    def test_html_with_no_extractable_text_falls_back_to_text(self) -> None:
        item = {
            "final_url": "http://example.com/empty",
            "html": "<html><head></head><body></body></html>",
            "text": "fallback body text",
        }
        markdown, metadata = _format_crawl_item(item)
        assert markdown == "fallback body text"

    def test_missing_text_returns_none_markdown(self) -> None:
        item = {"final_url": "http://example.com/x"}
        markdown, metadata = _format_crawl_item(item)
        assert markdown is None
        assert metadata == {}
