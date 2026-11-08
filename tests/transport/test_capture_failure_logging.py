"""#655: browser extraction failures surface in the logs, not silently as empty pages."""

from __future__ import annotations

import logging

from crawlutron.transport.browser import BrowserRenderer
from tests._browser_page_doubles import BrowserFakePage


class _FailingPage(BrowserFakePage):
    """A page whose ``content``/``evaluate`` raise, as a wedged renderer would."""

    def __init__(self) -> None:
        super().__init__()
        self._error = RuntimeError("renderer wedged")

    async def content(self):
        raise self._error

    async def evaluate(self, *a, **k):
        raise self._error


async def test_extract_html_failure_is_logged_not_silent(caplog):
    """#655: page.content() blowing up must surface a WARNING naming the exception
    class, not silently yield an empty page."""
    with caplog.at_level(logging.WARNING, logger="crawlutron.transport.browser"):
        assert await BrowserRenderer.extract_html(_FailingPage()) == ""
    assert any("html_capture_failed" in r.message for r in caplog.records), caplog.text


async def test_extract_text_failure_is_logged_not_silent(caplog):
    with caplog.at_level(logging.WARNING, logger="crawlutron.transport.browser"):
        assert await BrowserRenderer.extract_text(_FailingPage()) == ""
    assert any("text_capture_failed" in r.message for r in caplog.records), caplog.text
