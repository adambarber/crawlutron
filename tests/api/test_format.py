"""Unified output formatting: every crawl strategy funnels through one path."""

from crawlutron.service.unified_shaping import format_crawl_item
from tests._result_builders import engine_result


class TestFormatCrawlItemUnified:
    def test_html_result_uses_markdownify(self) -> None:
        import crawlutron_extract

        html = "<html><head><title>T</title></head><body><p>Body text here</p></body></html>"
        item = engine_result(
            "http://example.com/",
            "rendered",
            html=html,
            markdown=crawlutron_extract.extract(
                html.encode("utf-8"), "text/html", url="http://example.com/"
            ).markdown,
            final_url="http://example.com/",
        )
        markdown, metadata = format_crawl_item(item)
        assert markdown is not None
        assert "Body text here" in markdown
        assert metadata.get("title") == "T"
        assert metadata.get("source_url") == "http://example.com/"

    def test_text_only_result_returns_none_markdown_by_default(self) -> None:
        item = engine_result(
            "http://example.com/doc.pdf",
            "rendered",
            text="extracted pdf text",
            final_url="http://example.com/doc.pdf",
        )
        markdown, metadata = format_crawl_item(item)
        # Markdown is opt-in: text never masquerades as the markdown output.
        assert markdown is None
        assert metadata == {}

    def test_html_with_no_extractable_text_returns_none_markdown_by_default(self) -> None:
        item = engine_result(
            "http://example.com/empty",
            "rendered",
            html="<html><head></head><body></body></html>",
            text="fallback body text",
            final_url="http://example.com/empty",
        )
        markdown, metadata = format_crawl_item(item)
        # Markdown stays None by default, but metadata still parses from the HTML.
        assert markdown is None
        assert metadata.get("source_url") == "http://example.com/empty"

    def test_document_markdown_without_html_returns_empty_metadata(self) -> None:
        item = engine_result(
            "http://example.com/doc.csv",
            "rendered",
            markdown="# Heading\n\nbody",
            final_url="http://example.com/doc.csv",
        )
        markdown, metadata = format_crawl_item(item)
        assert markdown == "# Heading\n\nbody"
        assert metadata == {}

    def test_missing_text_returns_none_markdown(self) -> None:
        item = engine_result("http://example.com/x", "rendered", final_url="http://example.com/x")
        markdown, metadata = format_crawl_item(item)
        assert markdown is None
        assert metadata == {}

    def test_build_unified_result_wire_markdown_none_by_default(self) -> None:
        """The wire carries markdown None when the caller did not ask for it;
        text and html are still delivered."""
        from crawlutron.service.unified_shaping import build_unified_result

        item = engine_result(
            "http://example.com/x",
            "rendered",
            html="<html><body>Example Org</body></html>",
            text="Example Org",
            final_url="http://example.com/x",
        )
        result = build_unified_result(item)
        assert result.markdown is None
        assert result.text == "Example Org"
        assert result.html == "<html><body>Example Org</body></html>"

    def test_build_unified_result_carries_markdown_when_record_has_it(self) -> None:
        """A record that carries markdown projects it onto the wire verbatim."""
        from crawlutron.service.unified_shaping import build_unified_result

        item = engine_result(
            "http://example.com/x",
            "rendered",
            html="<html><body>Example Org</body></html>",
            text="Example Org",
            markdown="# Example Org",
            final_url="http://example.com/x",
        )
        result = build_unified_result(item)
        assert result.markdown == "# Example Org"
        assert result.text == "Example Org"

    def test_default_caller_keeps_metadata_when_markdown_none(self) -> None:
        """Default caller (no include_markdown): wire markdown is None but metadata
        from carried llm_metadata still populates on the wire."""
        from crawlutron.service.unified_shaping import build_unified_result

        item = engine_result(
            "http://example.com/x",
            "rendered",
            html="<html><head><title>T</title></head><body>Example Org</body></html>",
            text="Example Org",
            markdown=None,
            llm_metadata={"title": "T", "description": "d"},
            final_url="http://example.com/x",
        )
        markdown, metadata = format_crawl_item(item)
        assert markdown is None
        assert metadata.get("title") == "T"
        assert metadata.get("description") == "d"
        result = build_unified_result(item)
        assert result.markdown is None
        assert result.metadata.title == "T"
        assert result.metadata.description == "d"
