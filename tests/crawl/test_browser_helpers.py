import pytest

pytestmark = pytest.mark.browser

"""Real tests for browser.py logic (no browser; inject failing factories)."""


import os
import tempfile
from pathlib import Path

os.environ.setdefault(
    "BROWSER_CRAWLER_LOG_FILE", str(Path(tempfile.gettempdir()) / "browser-crawler-test.jsonl")
)

from app.crawl.strategies.browser import BrowserRenderer
from app.models import RenderOptions


def _raise_timeout(**kwargs: object):
    raise TimeoutError("navigation timeout")


def _raise_generic(**kwargs: object):
    raise RuntimeError("boom")


class TestBrowserRenderRetry:
    async def test_render_exhausts_retries_on_timeout_error(self):
        renderer = BrowserRenderer(browser_factory=_raise_timeout, sleep=lambda _s: None)
        result = await renderer.render("http://example.com/", RenderOptions(max_retries=1))
        assert result.status == "retry_exhausted"
        assert result.classification == "navigation_timeout"
        assert result.attempts == 2

    async def test_render_exhausts_retries_on_generic_error(self):
        renderer = BrowserRenderer(browser_factory=_raise_generic, sleep=lambda _s: None)
        result = await renderer.render("http://example.com/", RenderOptions(max_retries=1))
        assert result.status == "retry_exhausted"
        assert result.classification == "unknown_error"
        assert result.attempts == 2


class TestBrowserKwargs:
    def test_default_viewport_is_desktop(self):
        renderer = BrowserRenderer()
        launch_kwargs = renderer._get_browser_kwargs({})
        assert "viewport" not in launch_kwargs
        assert renderer._get_page_kwargs({})["viewport"] == {"width": 1365, "height": 768}

    def test_mobile_viewport_is_applied(self):
        renderer = BrowserRenderer()
        assert renderer._get_page_kwargs({"viewport": "mobile"})["viewport"] == {
            "width": 390,
            "height": 844,
        }

    def test_reduced_resource_mode_disables_images(self):
        renderer = BrowserRenderer()
        assert renderer._get_browser_kwargs({"resource_mode": "reduced"})["args"] == [
            "--blink-settings=imagesEnabled=false"
        ]
