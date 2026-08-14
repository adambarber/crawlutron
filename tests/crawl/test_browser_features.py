"""Browser feature tests (reuse, action chains, targeted screenshots).

Uses a hand-rolled fake Playwright-compatible page (no real browser, no
unittest.mock) so the orchestration logic is exercised deterministically.
"""

import asyncio
import base64
import unittest

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

    def on(self, event, handler):
        self.calls.append(("on", event))

    async def goto(self, url, wait_until="load"):
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


class ActionChainTest(unittest.TestCase):
    def test_action_chain_dispatches_correctly(self):
        page = FakePage()
        renderer = BrowserRenderer(browser_factory=lambda **kw: FakeBrowserCM(FakeBrowser()))
        actions = [
            {"type": "click", "selector": "#go"},
            {"type": "fill", "selector": "#q", "value": "hello"},
            {"type": "hover", "selector": "#menu"},
            {"type": "scroll", "selector": "#bottom"},
            {"type": "scroll_bottom"},
            {"type": "wait", "selector": "#loaded", "timeout": 3000},
            {"type": "wait_seconds", "seconds": 0.0},
            {"type": "execute_js", "script": "window.x=1"},
        ]
        asyncio.run(renderer._run_action_chain(page, actions))
        called = {(c[0], c[1]) for c in page.calls}
        self.assertIn(("click", "#go"), called)
        self.assertIn(("fill", "#q", "hello"), page.calls)
        self.assertIn(("hover", "#menu"), called)
        self.assertIn(("wait_for_selector", "#loaded", 3000), page.calls)
        self.assertTrue(page.elements["#bottom"].scrolled)

    def test_action_chain_is_best_effort(self):
        page = FakePage()

        async def boom(selector=None):
            raise RuntimeError("nope")

        page.click = boom
        renderer = BrowserRenderer(browser_factory=lambda **kw: FakeBrowserCM(FakeBrowser()))
        # Should not raise despite the failing action.
        asyncio.run(renderer._run_action_chain(page, [{"type": "click", "selector": "#x"}]))


class ElementScreenshotTest(unittest.TestCase):
    def test_capture_element_screenshots(self):
        page = FakePage()
        renderer = BrowserRenderer(browser_factory=lambda **kw: FakeBrowserCM(FakeBrowser()))
        shots = asyncio.run(renderer._capture_element_screenshots(page, ["#a", "#missing"]))
        selectors = {s["selector"] for s in shots}
        self.assertIn("#a", selectors)
        self.assertNotIn("#missing", selectors)
        for s in shots:
            decoded = base64.b64decode(s["screenshot"])
            self.assertEqual(decoded, b"elementpng")


class BrowserReuseTest(unittest.TestCase):
    def test_single_browser_reused_across_renders(self):
        browser = FakeBrowser()
        cm = FakeBrowserCM(browser)
        renderer = BrowserRenderer(browser_factory=lambda **kw: cm, sleep=lambda _s: None)
        opts = RenderOptions(browser_profile={})
        # Two renders should reuse the SAME browser instance.
        asyncio.run(renderer.render("http://example.com/", opts))
        self.assertFalse(renderer._browser_reused)
        asyncio.run(renderer.render("http://example.com/page", opts))
        self.assertTrue(renderer._browser_reused)
        # The browser context manager is entered only once.
        self.assertEqual(cm.enters, 1)
        self.assertEqual(browser.pages, 2)

    def test_close_releases_browser(self):
        browser = FakeBrowser()
        cm = FakeBrowserCM(browser)
        renderer = BrowserRenderer(browser_factory=lambda **kw: cm, sleep=lambda _s: None)
        asyncio.run(renderer.render("http://example.com/", RenderOptions(browser_profile={})))
        asyncio.run(renderer.close())
        self.assertIsNone(renderer._browser)
        self.assertIsNone(renderer._browser_cm)

    def test_render_still_succeeds_with_fake_browser(self):
        browser = FakeBrowser()
        cm = FakeBrowserCM(browser)
        renderer = BrowserRenderer(browser_factory=lambda **kw: cm, sleep=lambda _s: None)
        result = asyncio.run(renderer.render("http://example.com/", RenderOptions(browser_profile={})))
        self.assertEqual(result.status, "success")
        self.assertIn("Visible body text", result.text or "")


if __name__ == "__main__":
    unittest.main()
