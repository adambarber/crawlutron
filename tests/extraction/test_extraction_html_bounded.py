"""The full-page HTML pass delegates to crawlutron_extract.html_pass; oversized input
is refused before any parse runs."""

from __future__ import annotations

import logging

from crawlutron.extraction.documents import (
    EXTRACTION_DOC_MAX_INPUT_BYTES,
    ExtractedContent,
    extract_html,
)
from tests._policy_factories import canonical


def test_extract_html_refuses_oversized_html(monkeypatch, caplog):
    """HTML past the extraction ceiling is refused before the package parses it."""

    huge = "<html><body>" + "<p>x</p>" * (EXTRACTION_DOC_MAX_INPUT_BYTES // 6 + 1000) + "</body></html>"
    assert len(huge) > EXTRACTION_DOC_MAX_INPUT_BYTES
    with caplog.at_level(logging.WARNING, logger="crawlutron.extraction.documents"):
        result = extract_html(huge, canonical("http://example.com/"))
    assert result == ExtractedContent(None, None)
    assert "extraction_html_payload_too_large" in caplog.text, caplog.text


def test_extract_html_full_page_comes_from_crawlutron_extract(caplog):
    """Full-page HTML is produced by crawlutron_extract.html_pass (single bounded parse,
    ExtractLimits); the adapter mirrors its text/markdown exactly."""
    import crawlutron_extract
    from crawlutron_extract.limits import ExtractLimits

    html = "<html><body>" + "<p>x</p>" * 500 + "</body></html>"
    expected = crawlutron_extract.html_pass.extract_html(html, ExtractLimits())
    with caplog.at_level(logging.WARNING, logger="crawlutron.extraction.documents"):
        result = extract_html(html, canonical("http://example.com/"))
    assert result.text == expected["text"]
    assert result.markdown == expected["markdown"]
