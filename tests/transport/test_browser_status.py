"""Tests for browser.py module."""

from crawlutron.transport.browser import BrowserRenderer, looks_like_pdf


class TestBrowserRendererStatus:
    def test_looks_like_pdf(self):
        assert looks_like_pdf("https://example.com/document.pdf")
        assert looks_like_pdf("https://example.com/document.PDF")
        assert looks_like_pdf("https://example.com/path/file.pdf?query=1")
        assert not looks_like_pdf("https://example.com/document.html")
        assert not looks_like_pdf("https://example.com/")

    def test_headless_enabled_default(self, monkeypatch):
        monkeypatch.delenv("CRAWLUTRON_HEADLESS", raising=False)
        renderer = BrowserRenderer()
        assert renderer.driver.headless_enabled() is True

    def test_headless_enabled_false_values(self, monkeypatch):
        for val in ["0", "false", "no", "off", "FALSE"]:
            monkeypatch.setenv("CRAWLUTRON_HEADLESS", val)
            renderer = BrowserRenderer()
            assert renderer.driver.headless_enabled() is False
