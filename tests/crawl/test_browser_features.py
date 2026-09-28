"""Browser feature tests (reuse, action chains, targeted screenshots).

Drives the renderer over a fake Playwright-compatible page -- no real browser,
no ``unittest.mock`` -- so the orchestration logic is exercised deterministically.
That is also why this file carries no ``browser`` marker: the marker means
"launches a real headless browser", and every test here runs against a double.
Marking it excluded six unit tests from the default suite and from the
pre-commit hook, so a change to the renderer's page surface could break them
with nothing red.
"""

import base64

from app.models import RenderOptions
from tests._browser_doubles import BrowserFakeContextManager, ScriptedPage, scripted_renderer
from tests._policy_factories import UNRESTRICTED_HOST_POLICY


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


class FakePage(ScriptedPage):
    """A scripted page plus the element/interaction surface these tests drive.

    The navigation, evaluation and content half is :class:`ScriptedPage`; what
    stays here is the part specific to this suite -- a keyed element store, the
    interaction calls the renderer records, and the response listener the XHR
    interception installs.
    """

    def __init__(self):
        super().__init__()
        self.calls = []
        self.elements = {}
        self.responses = []
        self.extra_http_headers = {}

    async def route(self, pattern, handler):
        self.route_calls.append((pattern, handler))

    async def set_extra_http_headers(self, headers):
        self.extra_http_headers = headers

    def on(self, event, handler):
        self.calls.append(("on", event))

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
        return self.elements.setdefault(selector, FakeElement(selector))

    async def screenshot(self, full_page=False):
        self.calls.append(("screenshot", full_page))
        return b"fakepngbytes"


class FakeBrowser:
    """Hands out a fresh page per tab and counts the tabs it has opened."""

    def __init__(self):
        self.pages = 0

    async def new_page(self, **kwargs):
        self.pages += 1
        return FakePage()

    @property
    def is_closed(self):
        return False


def _renderer():
    """A renderer over a fresh fake browser, for a test that keeps no handle on it."""
    return scripted_renderer(FakeBrowser())


def _reuse_renderer():
    """The same renderer, plus the browser and its context manager.

    A reuse assertion is about how many times those two were entered and how
    many pages they handed out, so the test has to be holding them.
    """
    browser = FakeBrowser()
    cm = BrowserFakeContextManager(browser)
    return browser, cm, scripted_renderer(browser, cm)


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
