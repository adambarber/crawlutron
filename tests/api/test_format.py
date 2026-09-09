"""Unified output formatting: every crawl strategy funnels through one path."""

from app.api.response_shaping import format_crawl_item


class TestFormatCrawlItemUnified:
    def test_html_result_uses_markdownify(self) -> None:
        from app.crawl.extraction import extract_content

        html = "<html><head><title>T</title></head><body><p>Body text here</p></body></html>"
        item = {
            "html": html,
            "markdown": extract_content(html, "text/html", "http://example.com/").markdown,
            "final_url": "http://example.com/",
        }
        markdown, metadata = format_crawl_item(item)
        assert markdown is not None
        assert "Body text here" in markdown
        assert metadata.get("title") == "T"
        assert metadata.get("source_url") == "http://example.com/"

    def test_text_only_result_falls_back_to_extracted_text(self) -> None:
        item = {
            "final_url": "http://example.com/doc.pdf",
            "text": "extracted pdf text",
        }
        markdown, metadata = format_crawl_item(item)
        assert markdown == "extracted pdf text"
        # No HTML to parse, so only the basic metadata survives.
        assert metadata == {}

    def test_html_with_no_extractable_text_falls_back_to_text(self) -> None:
        item = {
            "final_url": "http://example.com/empty",
            "html": "<html><head></head><body></body></html>",
            "text": "fallback body text",
        }
        markdown, metadata = format_crawl_item(item)
        assert markdown == "fallback body text"

    def test_document_markdown_without_html_returns_empty_metadata(self) -> None:
        item = {"final_url": "http://example.com/doc.csv", "markdown": "# Heading\n\nbody"}
        markdown, metadata = format_crawl_item(item)
        assert markdown == "# Heading\n\nbody"
        assert metadata == {}

    def test_missing_text_returns_none_markdown(self) -> None:
        item = {"final_url": "http://example.com/x"}
        markdown, metadata = format_crawl_item(item)
        assert markdown is None
        assert metadata == {}
