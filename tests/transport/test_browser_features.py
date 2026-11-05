"""Browser feature tests (reuse, action chains, targeted screenshots).

Drives the renderer over a fake Playwright-compatible page, no mocks; no
``browser`` marker means these unit tests run in the pre-commit hook too.
"""

import base64

from crawlutron.models.render_options import RenderOptions
from crawlutron.transport.browser_driver import BrowserDriver
from crawlutron.vocabulary.browser import BrowserAction
from tests._browser_doubles import BrowserFakeContextManager, ScriptedPage, scripted_renderer
from tests._policy_factories import UNRESTRICTED_HOST_POLICY


class FakeElement:
    def __init__(self, selector):
        self.selector = selector
        self.scrolled = False

    async def scroll_into_view_if_needed(self):
        self.scrolled = True

    async def screenshot(self):
        return b"elementpng"


class FakePage(ScriptedPage):
    """A scripted page plus the element/interaction surface these tests drive.

    The navigation/evaluation/content half is ``ScriptedPage``; the rest here
    is the keyed element store, interaction calls, and the XHR listener.
    """

    def __init__(self):
        super().__init__()
        self.calls: list[tuple[object, ...]] = []
        self.elements: dict[str, FakeElement] = {}
        self.responses: list[object] = []
        # C3 red lever: the first goto raises this, later ones succeed.
        self.fail_first_goto: BaseException | None = None

    async def goto(self, url, wait_until="load", **kwargs):
        error, self.fail_first_goto = self.fail_first_goto, None
        if error is not None:
            raise error
        await super().goto(url, wait_until=wait_until, **kwargs)

    async def _click(self, selector: str) -> object:
        self.calls.append(("click", selector))
        return None

    click = _click

    def on(self, event, handler):
        self.calls.append(("on", event))

    async def fill(self, selector, value):
        self.calls.append(("fill", selector, value))

    async def hover(self, selector):
        self.calls.append(("hover", selector))

    async def wait_for_selector(self, selector, timeout=5000, **kwargs):
        self.calls.append(("wait_for_selector", selector, timeout))

    async def query_selector(self, selector):
        if "missing" in selector:
            return None
        return self.elements.setdefault(selector, FakeElement(selector))

    async def screenshot(self, **k):
        # ``**k`` to match :meth:`BrowserFakePage.screenshot`; the page-level flag is read out of it rather
        # than named, so a signature narrowed to ``full_page`` cannot reject the other kwargs the renderer
        self.calls.append(("screenshot", k.get("full_page", False)))
        return b"fakepngbytes"


class FakeBrowser:
    """Hands out a fresh page per tab and counts the tabs it has opened."""

    def __init__(self):
        self.pages = 0
        self.fail_first_goto: BaseException | None = None

    async def new_page(self, **kwargs):
        self.pages += 1
        page = FakePage()
        page.fail_first_goto = self.fail_first_goto
        self.fail_first_goto = None  # only the first attempt's first goto fails
        return page

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
    from tests._policy_factories import canonical

    return await renderer.render(
        canonical(f"http://example.com{path}"),
        RenderOptions(browser_profile={}),
        policy=UNRESTRICTED_HOST_POLICY,
    )


class TestActionChain:
    async def test_action_chain_dispatches_correctly(self):
        page = FakePage()
        driver = BrowserDriver()
        actions: list[BrowserAction] = [
            {"type": "click", "selector": "#go"},
            {"type": "fill", "selector": "#q", "value": "hello"},
            {"type": "hover", "selector": "#menu"},
            {"type": "scroll", "selector": "#bottom"},
            {"type": "scroll_bottom"},
            {"type": "wait", "selector": "#loaded", "timeout": 3000},
            {"type": "wait_seconds", "seconds": 0.0},
        ]
        await driver.run_action_chain(page, actions)
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

        async def fine(selector=None):
            page.calls.append(("hover", selector))

        # Instance overrides, not class edits: the click verb must fail on THIS page only.
        object.__setattr__(page, "click", boom)
        object.__setattr__(page, "hover", fine)
        await BrowserDriver().run_action_chain(
            page,
            [{"type": "click", "selector": "#x"}, {"type": "hover", "selector": "#after"}],
        )

        # One failed action must not cost the page its later actions.
        assert ("hover", "#after") in page.calls


class TestElementScreenshot:
    async def test_capture_element_screenshots(self):
        page = FakePage()
        shots = await BrowserDriver().capture_element_screenshots(page, ["#a", "#missing"])
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
        assert not renderer.driver.reused
        await _render(renderer, "/page")
        assert renderer.driver.reused
        assert cm.enters == 1
        assert browser.pages == 2

    async def test_a_transient_failure_retries_on_the_same_browser(self):
        """C3: a page-level exception is an attempt lifecycle, not a browser one.

        The retry loop must never tear down the browser between attempts: the
        driver's recycle bound and shutdown own that, so attempt 2 renders on the
        browser attempt 1 launched, entered exactly once."""
        browser, cm, renderer = _reuse_renderer()
        browser.fail_first_goto = TimeoutError("navigation timeout")
        result = await _render(renderer)
        assert result.status == "success"
        assert cm.enters == 1, "the browser was relaunched for the retry"
        assert browser.pages == 2

    async def test_close_releases_browser(self):
        _, cm, renderer = _reuse_renderer()
        await _render(renderer)
        await renderer.close()
        assert renderer.driver.browser is None
        # The handle going away is the observable; the context manager actually being exited is the part that
        # would otherwise leak a browser process.
        assert cm.exits == 1

    async def test_render_still_succeeds_with_fake_browser(self):
        result = await _render(_renderer())
        assert result.status == "success"
        assert "Visible body text" in (result.text or "")
