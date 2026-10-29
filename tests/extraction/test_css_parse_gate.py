"""The CSS parse itself must be bounded, not just the select (#521)."""

import pytest

from crawlutron.extraction.content import ExtractedContent, Selectors, extract_selected
from tests._extraction_helpers import settings_with


@pytest.mark.real_sleeps
def test_oversized_css_payload_is_refused_not_parsed(monkeypatch, caplog):
    """An input past the extraction ceiling must not reach BeautifulSoup at all:
    the parse runs untimed on the caller thread, so a hostile payload stalls it.
    """
    import logging

    import bs4

    def exploding_init(self, *args, **kwargs):
        raise AssertionError("BeautifulSoup was constructed for a refused payload")

    monkeypatch.setattr(bs4.BeautifulSoup, "__init__", exploding_init)
    import crawlutron.extraction.documents as docex

    huge = "<html><body>" + "<p>x</p>" * (docex.EXTRACTION_DOC_MAX_INPUT_BYTES // 6 + 1000) + "</body></html>"
    with caplog.at_level(logging.WARNING, logger="crawlutron.extraction.content"):
        result = extract_selected(
            huge,
            "http://example.com/",
            selectors=Selectors(css=".x"),
            settings=settings_with(extraction_selector_timeout=5.0),
        )
    assert result == ExtractedContent(None, None)
    assert "extraction_css_payload_too_large" in caplog.text, caplog.text
