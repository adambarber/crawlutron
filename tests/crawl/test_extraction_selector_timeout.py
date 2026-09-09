"""LOW fix: CSS/XPath selector evaluation must be wall-clock bounded.

A pathological or adversarial selector could otherwise run unbounded and tie up
a request worker. _extract_selected now evaluates selectors inside a worker
thread with a timeout (BROWSER_CRAWLER_EXTRACTION_SELECTOR_TIMEOUT, default 2s)
and returns a safe empty fallback on timeout.
"""

import time

import bs4

import app.crawl.extraction as extraction_mod
from app.crawl.extraction import ExtractedContent, _extract_selected


def test_css_selector_slow_must_not_hang(monkeypatch, real_sleeps):
    monkeypatch.setattr(extraction_mod, "_EXTRACTION_SELECTOR_TIMEOUT", 0.2)
    monkeypatch.setattr(
        bs4.BeautifulSoup,
        "select",
        lambda self, selector: time.sleep(3) or [],
    )
    html = "<html><body><p class='x'>hi</p></body></html>"
    start = time.monotonic()
    result = _extract_selected(html, "http://example.com/", css_selector=".x")
    elapsed = time.monotonic() - start
    assert elapsed < 1.0, "slow CSS selector was not bounded by the timeout"
    assert result == ExtractedContent(None, None)


def test_css_selector_fast_still_works(monkeypatch):
    monkeypatch.setattr(extraction_mod, "_EXTRACTION_SELECTOR_TIMEOUT", 0.2)
    html = "<html><body><p class='x'>hi</p></body></html>"
    result = _extract_selected(html, "http://example.com/", css_selector=".x")
    assert result.text is not None
    assert "hi" in (result.text or "")


class _SlowFakeTree:
    def xpath(self, expr):
        time.sleep(3)
        return []


def test_xpath_selector_slow_must_not_hang(monkeypatch, real_sleeps):
    monkeypatch.setattr(extraction_mod, "_EXTRACTION_SELECTOR_TIMEOUT", 0.2)
    monkeypatch.setattr(extraction_mod, "_parse_html_tree", lambda html: _SlowFakeTree())
    html = "<html><body><p>x</p></body></html>"
    start = time.monotonic()
    result = _extract_selected(html, "http://example.com/", xpath="//p")
    elapsed = time.monotonic() - start
    assert elapsed < 1.0, "slow XPath was not bounded by the timeout"
    assert result == ExtractedContent(None, None)


def test_xpath_selector_fast_still_works(monkeypatch):
    monkeypatch.setattr(extraction_mod, "_EXTRACTION_SELECTOR_TIMEOUT", 0.2)
    html = "<html><body><p>x</p></body></html>"
    result = _extract_selected(html, "http://example.com/", xpath="//p")
    assert result.text is not None
    assert "x" in (result.text or "")
