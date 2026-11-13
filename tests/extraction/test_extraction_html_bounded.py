"""The full-page trafilatura path is bounded: an input byte cap and a selector deadline."""

from __future__ import annotations

import logging
import threading

import trafilatura

from crawlutron.extraction.documents import (
    EXTRACTION_DOC_MAX_INPUT_BYTES,
    ExtractedContent,
    extract_html,
)
from tests._extraction_helpers import settings_with
from tests._policy_factories import canonical


def test_extract_html_refuses_oversized_html(monkeypatch, caplog):
    """HTML past the extraction ceiling is refused before trafilatura sees it."""

    def exploding(*args: object, **kwargs: object) -> str:
        raise AssertionError("trafilatura was invoked for a refused payload")

    monkeypatch.setattr(trafilatura, "extract", exploding)
    huge = "<html><body>" + "<p>x</p>" * (EXTRACTION_DOC_MAX_INPUT_BYTES // 6 + 1000) + "</body></html>"
    assert len(huge) > EXTRACTION_DOC_MAX_INPUT_BYTES
    with caplog.at_level(logging.WARNING, logger="crawlutron.extraction.documents"):
        result = extract_html(huge, canonical("http://example.com/"))
    assert result == ExtractedContent(None, None)
    assert "extraction_html_payload_too_large" in caplog.text, caplog.text


def test_extract_html_trafilatura_times_out_on_pathological_html(monkeypatch, caplog):
    """Trafilatura spinning past the selector deadline is abandoned; fallbacks get nothing."""
    stuck = threading.Event()
    monkeypatch.setattr(
        trafilatura,
        "extract",
        lambda *args, **kwargs: stuck.wait(timeout=30) or "",
    )
    try:
        with caplog.at_level(logging.WARNING, logger="crawlutron.extraction.documents"):
            result = extract_html(
                "<html><body></body></html>",
                canonical("http://example.com/"),
                settings=settings_with(extraction_selector_timeout=0.2),
            )
    finally:
        stuck.set()
    assert result == ExtractedContent(None, None)
    assert "extraction_selector_timeout" in caplog.text, caplog.text
