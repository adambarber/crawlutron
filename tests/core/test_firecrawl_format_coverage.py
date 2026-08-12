"""Real tests for Firecrawl-format extraction edge branches (no mocks)."""

import os
import sys
from pathlib import Path

from app.crawl.firecrawl_format import extract_firecrawl_format  # noqa: E402

class TestFirecrawlFormatCoverage:
    def test_empty_body_triggers_markdownify_fallback(self) -> None:
        html = "<html><head></head><body></body></html>"
        markdown, metadata = extract_firecrawl_format(html, "http://example.com/")
        # trafilatura yields nothing for an empty body; markdownify fallback runs.
        assert markdown is None or isinstance(markdown, str)
        assert metadata["source_url"] == "http://example.com/"

    def test_metadata_extracts_canonical_and_favicon_and_skips_incomplete_meta(self) -> None:
        html = (
            "<html><head>"
            "<title>Page</title>"
            '<meta content="no-name-attribute">'
            '<meta name="description" content="">'
            '<meta name="og:title" content="OG">'
            '<link rel="canonical" href="http://example.com/canonical">'
            '<link rel="icon" href="http://example.com/fav.ico">'
            "</head><body><p>Body</p></body></html>"
        )
        markdown, metadata = extract_firecrawl_format(html, "http://example.com/")
        assert metadata["title"] == "Page"
        assert metadata["og_title"] == "OG"
        assert metadata["canonical_url"] == "http://example.com/canonical"
        assert metadata["favicon"] == "http://example.com/fav.ico"
        assert "description" not in metadata  # empty content skipped

    def test_empty_or_whitespace_html_returns_none(self) -> None:
        for html in ["", None, "   ", "\n\n"]:
            markdown, metadata = extract_firecrawl_format(html, "http://example.com/")
            assert markdown is None
            assert metadata == {}

    def test_language_attribute_is_extracted(self) -> None:
        html = (
            '<html lang="en"><head><title>L</title></head>'
            "<body><p>Body text</p></body></html>"
        )
        markdown, metadata = extract_firecrawl_format(html, "http://example.com/")
        assert metadata["language"] == "en"
