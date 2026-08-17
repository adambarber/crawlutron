"""Tests for browser.py module."""

from app.browser import (
    BLOCKED_CLASSIFICATIONS,
    FAILED_CLASSIFICATIONS,
    BrowserRenderer,
)

class TestBrowserRendererStatus:
    def test_status_for_classification_rendered(self):
        renderer = BrowserRenderer()
        assert renderer._status_for_classification("rendered") == "success"

    def test_status_for_classification_blocked(self):
        renderer = BrowserRenderer()
        for classification in BLOCKED_CLASSIFICATIONS:
            assert renderer._status_for_classification(classification) == "blocked"

    def test_status_for_classification_failed(self):
        renderer = BrowserRenderer()
        for classification in FAILED_CLASSIFICATIONS:
            assert renderer._status_for_classification(classification) == "failed"

    def test_status_for_classification_unknown_defaults_to_blocked(self):
        renderer = BrowserRenderer()
        assert renderer._status_for_classification("unknown_classification") == "blocked"

    def test_looks_like_pdf(self):
        renderer = BrowserRenderer()
        assert renderer._looks_like_pdf("https://example.com/document.pdf")
        assert renderer._looks_like_pdf("https://example.com/document.PDF")
        assert renderer._looks_like_pdf("https://example.com/path/file.pdf?query=1")
        assert not renderer._looks_like_pdf("https://example.com/document.html")
        assert not renderer._looks_like_pdf("https://example.com/")

    def test_headless_enabled_default(self):
        renderer = BrowserRenderer()
        assert renderer._headless_enabled() is True

    def test_headless_enabled_false_values(self):
        import os
        for val in ["0", "false", "no", "off", "FALSE"]:
            os.environ["BROWSER_CRAWLER_HEADLESS"] = val
            try:
                renderer = BrowserRenderer()
                assert renderer._headless_enabled() is False
            finally:
                del os.environ["BROWSER_CRAWLER_HEADLESS"]
