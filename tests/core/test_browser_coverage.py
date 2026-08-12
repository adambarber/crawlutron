"""Real tests for browser.py logic (no browser; inject failing factories)."""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("BROWSER_CRAWLER_LOG_FILE", str(Path(tempfile.gettempdir()) / "browser-crawler-test.jsonl"))

from app.browser import BrowserRenderer  # noqa: E402
from app.models import RenderOptions  # noqa: E402

def _raise_timeout(**kwargs: object):
    raise TimeoutError("navigation timeout")

def _raise_generic(**kwargs: object):
    raise RuntimeError("boom")

class TestBrowserRenderRetry:
    def test_render_exhausts_retries_on_timeout_error(self) -> None:
        renderer = BrowserRenderer(browser_factory=_raise_timeout, sleep=lambda _s: None)
        result = asyncio.run(renderer.render("http://example.com/", RenderOptions(max_retries=1)))
        assert result.status == "retry_exhausted"
        assert result.classification == "navigation_timeout"
        assert result.attempts == 2

    def test_render_exhausts_retries_on_generic_error(self) -> None:
        renderer = BrowserRenderer(browser_factory=_raise_generic, sleep=lambda _s: None)
        result = asyncio.run(renderer.render("http://example.com/", RenderOptions(max_retries=1)))
        assert result.status == "retry_exhausted"
        assert result.classification == "unknown_error"
        assert result.attempts == 2

    def test_render_fails_without_browser_available(self) -> None:
        # Default factory requires Camoufox, which is absent in the test env.
        renderer = BrowserRenderer()
        result = asyncio.run(renderer.render("http://example.com/", RenderOptions(max_retries=0)))
        assert result.status == "retry_exhausted"
        assert result.classification == "unknown_error"

class TestBrowserKwargs:
    def test_default_viewport_is_desktop(self) -> None:
        renderer = BrowserRenderer()
        kwargs = renderer._get_browser_kwargs({})
        assert kwargs["viewport"] == {"width": 1365, "height": 768}
        assert "browser_args" not in kwargs

    def test_mobile_viewport_is_applied(self) -> None:
        renderer = BrowserRenderer()
        kwargs = renderer._get_browser_kwargs({"viewport": "mobile"})
        assert kwargs["viewport"] == {"width": 390, "height": 844}

    def test_reduced_resource_mode_disables_images(self) -> None:
        renderer = BrowserRenderer()
        kwargs = renderer._get_browser_kwargs({"resource_mode": "reduced"})
        assert kwargs["browser_args"] == ["--blink-settings=imagesEnabled=false"]
