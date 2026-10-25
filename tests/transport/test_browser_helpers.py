"""Real tests for the browser strategy's logic (no browser; inject failing factories)."""

from app.models.normalized_url import NormalizedUrl
from app.models.render_options import RenderOptions
from app.transport.browser import BrowserRenderer
from app.transport.browser_driver import BrowserDriver
from tests._browser_page_doubles import BrowserFakeContextManager, BrowserFakePage
from tests._policy_factories import UNRESTRICTED_HOST_POLICY


def _raise_timeout(**kwargs: object):
    raise TimeoutError("navigation timeout")


def _raise_generic(**kwargs: object):
    raise RuntimeError("boom")


async def _retry_exhausted(browser_factory, *, max_retries: int = 1):
    """Render through a browser factory that always raises, retries off the clock.

    Asserts the budget was actually spent -- ``attempts == max_retries + 1`` --
    so "exhausted" cannot pass because the first attempt already failed.
    """
    renderer = BrowserRenderer(
        driver=BrowserDriver(browser_factory=browser_factory),
        sleep=lambda _s: None,
    )
    result = await renderer.render(
        NormalizedUrl("http://example.com/", policy=UNRESTRICTED_HOST_POLICY),
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


class TestServiceWorkersBlocked:
    def test_service_workers_blocked_at_page_open(self):
        """A hostile service worker's fetch() never crosses page.route, so SWs
        must be blocked at context creation -- new_page() owns that option.
        """
        driver = BrowserDriver()
        kwargs = driver.page_kwargs({})
        assert kwargs.get("service_workers") == "block", kwargs

    def test_service_workers_block_survives_profile_kwargs(self):
        driver = BrowserDriver()
        kwargs = driver.page_kwargs({"viewport": "mobile"})
        assert kwargs.get("service_workers") == "block", kwargs

    async def test_new_page_receives_service_workers_block(self):
        """The option must ride new_page's context options, not just sit in the
        kwargs dict -- the browser double records what actually arrived."""

        class _RecordingBrowser:
            def __init__(self):
                self.page_kwargs_seen: list[dict] = []

            async def new_page(self, **kwargs):
                self.page_kwargs_seen.append(kwargs)
                return BrowserFakePage()

            @property
            def is_closed(self):
                return False

        browser = _RecordingBrowser()
        driver = BrowserDriver(browser_factory=lambda **_kw: BrowserFakeContextManager(browser))
        await driver.open_page({})
        assert browser.page_kwargs_seen, "open_page never reached new_page"
        assert browser.page_kwargs_seen[0].get("service_workers") == "block"


class TestBrowserKwargs:
    def test_default_viewport_is_desktop(self):
        driver = BrowserDriver()
        launch_kwargs = driver.browser_kwargs({})
        assert "viewport" not in launch_kwargs
        assert driver.page_kwargs({})["viewport"] == {"width": 1365, "height": 768}

    def test_mobile_viewport_is_applied(self):
        driver = BrowserDriver()
        assert driver.page_kwargs({"viewport": "mobile"})["viewport"] == {
            "width": 390,
            "height": 844,
        }

    def test_reduced_resource_mode_disables_images(self):
        driver = BrowserDriver()
        assert driver.browser_kwargs({"resource_mode": "reduced"})["args"] == [
            "--blink-settings=imagesEnabled=false"
        ]
