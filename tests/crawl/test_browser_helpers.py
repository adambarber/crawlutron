"""Real tests for browser.py logic (no browser; inject failing factories)."""

from app.crawl.strategies.browser import BrowserRenderer
from app.models import RenderOptions
from tests._policy_factories import UNRESTRICTED_HOST_POLICY


def _raise_timeout(**kwargs: object):
    raise TimeoutError("navigation timeout")


def _raise_generic(**kwargs: object):
    raise RuntimeError("boom")


async def _retry_exhausted(browser_factory, *, max_retries: int = 1):
    """Render through a browser factory that always raises, retries off the clock.

    Asserts the budget was actually spent -- ``attempts == max_retries + 1`` --
    so a test claiming "exhausted" cannot pass because the first attempt already
    failed. Returns the result for the caller to classify.
    """
    renderer = BrowserRenderer(browser_factory=browser_factory, sleep=lambda _s: None)
    result = await renderer.render(
        "http://example.com/",
        RenderOptions(max_retries=max_retries),
        policy=UNRESTRICTED_HOST_POLICY,
    )
    assert result.status == "retry_exhausted", result.status
    assert result.attempts == max_retries + 1, result.attempts
    return result


class TestBrowserRenderRetry:
    async def test_render_exhausts_retries_on_timeout_error(self):
        result = await _retry_exhausted(_raise_timeout)
        assert result.classification == "navigation_timeout"

    async def test_render_exhausts_retries_on_generic_error(self):
        result = await _retry_exhausted(_raise_generic)
        assert result.classification == "unknown_error"


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
