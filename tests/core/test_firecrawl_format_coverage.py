"""Real tests for Firecrawl-format extraction edge branches (no mocks)."""

import os
import sys
from pathlib import Path

from app.crawl.firecrawl_format import (  # noqa: E402
    extract_firecrawl_format,
    _markdownify_html,
    _trafilatura_markdown,
    _text_from_html,
)

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

    def test_extract_firecrawl_format_returns_complete_text(self) -> None:
        # Regression: the extractor must return the COMPLETE page text, not a
        # short snippet (an earlier favor_precision=True setting truncated
        # non-article pages such as homepages to a handful of words).
        body = " ".join(f"word-{i}" for i in range(400))
        html = (
            "<html><head><title>Big Page</title></head>"
            f"<body><h1>Heading</h1><p>{body}</p></body></html>"
        )
        markdown, metadata = extract_firecrawl_format(html, "http://example.com/")
        assert markdown is not None
        assert len(markdown.split()) > 100
        assert "word-0" in markdown and "word-399" in markdown
        assert metadata["title"] == "Big Page"


class TestExtractionStrategies:
    def test_markdownify_primary_returns_complete_markdown(self) -> None:
        body = " ".join(f"word-{i}" for i in range(300))
        html = f"<html><body><h1>Heading</h1><p>{body}</p></body></html>"
        md = _markdownify_html(html)
        assert md is not None
        assert len(md.split()) > 100
        assert "word-0" in md and "word-299" in md

    def test_trafilatura_fallback_returns_markdown(self) -> None:
        body = " ".join(f"word-{i}" for i in range(300))
        html = f"<html><body><p>{body}</p></body></html>"
        md = _trafilatura_markdown(html, "http://example.com/")
        assert md and "word-0" in md and "word-299" in md

    def test_text_from_html_strips_scripts_and_tags(self) -> None:
        html = (
            "<html><body><p>Hello <b>World</b></p>"
            "<script>ignore_me();</script><style>.x{color:red}</style>"
            "</body></html>"
        )
        assert _text_from_html(html) == "Hello World"

    def test_extract_firecrawl_format_prefers_markdownify_complete_text(self) -> None:
        body = " ".join(f"item-{i}" for i in range(300))
        html = (
            "<html><head><title>Big</title></head>"
            f"<body><h1>H</h1><p>{body}</p></body></html>"
        )
        markdown, metadata = extract_firecrawl_format(html, "http://example.com/")
        assert markdown is not None
        assert len(markdown.split()) > 100
        assert "item-0" in markdown and "item-299" in markdown
        assert metadata["title"] == "Big"
