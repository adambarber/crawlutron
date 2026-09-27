import pytest

pytestmark = pytest.mark.browser

"""Browser feature tests (reuse, action chains, targeted screenshots).

Uses a hand-rolled fake Playwright-compatible page (no real browser, no
unittest.mock) so the orchestration logic is exercised deterministically.
"""


import base64

from _policy_factories import UNRESTRICTED_HOST_POLICY

from app.crawl.strategies.browser import BrowserRenderer
from app.models import RenderOptions


class FakeElement:
    def __init__(self, selector):
        self.selector = selector
        self.scrolled = False
        self.screenshots = 0

    async def scroll_into_view_if_needed(self):
        self.scrolled = True

    async def screenshot(self):
        self.screenshots += 1
        return b"elementpng"


class FakePage:
    def __init__(self):
        self._url = ""
        self.calls = []
        self.elements = {}
        self.responses = []
        self.extra_http_headers = {}

    def on(self, event, handler):
        self.calls.append(("on", event))

    async def route(self, pattern, handler):
        # Mirrors the real (async) Playwright page.route coroutine.
        self.calls.append(("route", pattern))

    async def set_extra_http_headers(self, headers):
        self.extra_http_headers = headers

    async def goto(self, url, wait_until="load", **kwargs):
        # Mirror the real Playwright/Camoufox goto signature (accepts
        # extra_http_headers etc.); the production code passes extra_http_headers.
        if "extra_http_headers" in kwargs:
            raise TypeError("unexpected keyword argument 'extra_http_headers'")
        self._url = url
        self.calls.append(("goto", url))

    @property
    def url(self):
        return self._url

    async def evaluate(self, expression):
        if "readyState" in expression:
            return "complete"
        if "document.title" in expression:
            return "Fake Title"
        if "innerText" in expression:
            return "Visible body text"
        if "scrollTo" in expression:
            return None
        return ""

    async def content(self):
        return "<html><body><h1>Visible body text</h1></body></html>"

    async def click(self, selector):
        self.calls.append(("click", selector))

    async def fill(self, selector, value):
        self.calls.append(("fill", selector, value))

    async def hover(self, selector):
        self.calls.append(("hover", selector))

    async def wait_for_selector(self, selector, timeout=5000):
        self.calls.append(("wait_for_selector", selector, timeout))

    async def query_selector(self, selector):
        if "missing" in selector:
            return None
        el = self.elements.setdefault(selector, FakeElement(selector))
        return el

    async def screenshot(self, full_page=False):
        self.calls.append(("screenshot", full_page))
        return b"fakepngbytes"

    @property
    def is_closed(self):
        return False


class FakeBrowser:
    def __init__(self):
        self.pages = 0

    async def new_page(self, **kwargs):
        self.pages += 1
        return FakePage()

    @property
    def is_closed(self):
        return False


class FakeBrowserCM:
    def __init__(self, browser):
        self.browser = browser
        self.enters = 0

    async def __aenter__(self):
        self.enters += 1
        return self.browser

    async def __aexit__(self, *exc):
        return False


def _renderer():
    """A renderer over a fresh fake browser, for a test that keeps no handle on it.

    ``sleep=lambda _s: None`` is the renderer's own wait budget, stubbed the way
    the suite stubs every other sleep: a feature test here is about what the
    render orchestrates, not how long it waited.
    """
    return BrowserRenderer(browser_factory=lambda **kw: FakeBrowserCM(FakeBrowser()), sleep=lambda _s: None)


def _reuse_renderer():
    """The same renderer, plus the browser and its context manager.

    A reuse assertion is about how many times those two were entered and how
    many pages they handed out, so the test has to be holding them.
    """
    browser = FakeBrowser()
    cm = FakeBrowserCM(browser)
    return browser, cm, BrowserRenderer(browser_factory=lambda **kw: cm, sleep=lambda _s: None)


async def _render(renderer, path: str = "/"):
    """One render at the empty browser profile, which is what these tests mean."""
    return await renderer.render(
        f"http://example.com{path}",
        RenderOptions(browser_profile={}),
        policy=UNRESTRICTED_HOST_POLICY,
    )


class TestActionChain:
    async def test_action_chain_dispatches_correctly(self):
        page = FakePage()
        renderer = _renderer()
        actions = [
            {"type": "click", "selector": "#go"},
            {"type": "fill", "selector": "#q", "value": "hello"},
            {"type": "hover", "selector": "#menu"},
            {"type": "scroll", "selector": "#bottom"},
            {"type": "scroll_bottom"},
            {"type": "wait", "selector": "#loaded", "timeout": 3000},
            {"type": "wait_seconds", "seconds": 0.0},
        ]
        await renderer._run_action_chain(page, actions)
        called = {(c[0], c[1]) for c in page.calls}
        assert ("click", "#go") in called
        assert ("fill", "#q", "hello") in page.calls
        assert ("hover", "#menu") in called
        assert ("wait_for_selector", "#loaded", 3000) in page.calls
        assert page.elements["#bottom"].scrolled

    async def test_action_chain_is_best_effort(self):
        page = FakePage()

        async def boom(selector=None):
            raise RuntimeError("nope")

        page.click = boom
        renderer = _renderer()
        await renderer._run_action_chain(page, [{"type": "click", "selector": "#x"}])


class TestElementScreenshot:
    async def test_capture_element_screenshots(self):
        page = FakePage()
        renderer = _renderer()
        shots = await renderer._capture_element_screenshots(page, ["#a", "#missing"])
        selectors = {s["selector"] for s in shots}
        assert "#a" in selectors
        assert "#missing" not in selectors
        for s in shots:
            decoded = base64.b64decode(s["screenshot"])
            assert decoded == b"elementpng"


class TestBrowserReuse:
    async def test_single_browser_reused_across_renders(self):
        browser, cm, renderer = _reuse_renderer()
        await _render(renderer)
        assert not renderer._browser_reused
        await _render(renderer, "/page")
        assert renderer._browser_reused
        assert cm.enters == 1
        assert browser.pages == 2

    async def test_close_releases_browser(self):
        _, _, renderer = _reuse_renderer()
        await _render(renderer)
        await renderer.close()
        assert renderer._browser is None
        assert renderer._browser_cm is None

    async def test_render_still_succeeds_with_fake_browser(self):
        result = await _render(_renderer())
        assert result.status == "success"
        assert "Visible body text" in (result.text or "")
