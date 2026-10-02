"""The cookie harvest must complete before the render returns.

The harvest used to fire-and-forget a collection task, so the cffi retry's
synchronous store read usually raced it and retried cookieless. Awaiting
the harvest makes the browser-minted session visible to the immediate
retry that follows the render.
"""

import asyncio
from contextlib import contextmanager

from app.crawl.strategies import browser as browser_mod
from app.crawl.strategies.browser import BrowserRenderer
from app.crawl.strategies.browser_driver import BrowserDriver
from app.models.registrable_domain import registrable_domain
from tests._browser_doubles import BrowserFakePage, FakeBrowserDriver, ScriptedPage, build_pinned_renderer
from tests._policy_factories import UNRESTRICTED_HOST_POLICY


def _page_with_context():
    """A page whose cookie store answers with one session cookie."""
    return ScriptedPage(context=_FakeContext())


class _FakeContext:
    async def cookies(self, url):
        # A scheduling point, not a duration: the point of the test is that the
        # harvest is AWAITED, so the assertion is on what the synchronous store
        # read sees after the await returns.
        await asyncio.sleep(0)
        return [{"name": "sess", "value": "abc123"}]


@contextmanager
def isolated_browser_cookies(url: str):
    """Detach the module-global cookie slot for ``url``'s domain, then restore it.

    ``_BROWSER_COOKIES_BY_DOMAIN`` is process-global and keyed by registrable
    domain, so a test that harvests or refuses to harvest against
    ``http://harvest-race.test/page`` is reading and writing state that outlives
    it. The pop/else-restore dance was written out per test, and the ``else``
    branch is exactly the part that goes missing: a test that pops the slot and
    never puts it back leaves a real crawler's cookies deleted.
    """
    domain = registrable_domain(url)
    old = browser_mod._BROWSER_COOKIES_BY_DOMAIN.pop(domain, None)
    try:
        yield domain
    finally:
        if old is None:
            browser_mod._BROWSER_COOKIES_BY_DOMAIN.pop(domain, None)
        else:
            browser_mod._BROWSER_COOKIES_BY_DOMAIN[domain] = old


class TestCookieHarvestCompletes:
    async def test_harvest_visible_to_immediate_retry(self):
        url = "http://harvest-race.test/page"
        with isolated_browser_cookies(url) as domain:
            renderer = BrowserRenderer()
            await renderer._harvest_session_cookies(_page_with_context(), url)
            # The synchronous read the cffi retry performs must see cookies NOW.
            assert browser_mod.get_browser_cookies_for(domain) == "sess=abc123"


class _WedgedContext:
    """Cookies() that never resolves (simulates a wedged transport)."""

    # The declared return is what this WOULD answer if it ever did. Left
    # undeclared, the signature inferred ``Coroutine[..., None]`` and the class
    # stopped satisfying the ``PageCookieContext`` contract production reads it
    # through -- a wedged context is a slow one, not a differently-shaped one.
    async def cookies(self, url: str | None = None) -> list[dict[str, str]]:
        await asyncio.Event().wait()
        raise AssertionError("unreachable: this coroutine never resolves")


class TestHarvestWaitBounded:
    """A wedged cookies() must not hold the pooled renderer for the
    whole per-URL budget; the finally-block caps the harvest wait."""

    async def test_render_finally_harvest_timeout(self, monkeypatch):
        from app.crawl.strategies.browser import RenderOptions

        url = "http://example.com/page"
        with isolated_browser_cookies(url) as domain:
            page = BrowserFakePage(context=_WedgedContext())
            close_calls: list[bool] = []

            async def record_close(closed_page):
                close_calls.append(closed_page)

            renderer = build_pinned_renderer(
                page,
                monkeypatch,
                driver=FakeBrowserDriver(page, harvest_timeout=0.05, on_close=record_close),
            )
            # The cap is the thing under test, so the driver reports the short
            # deadline rather than the five-second default. The deadline is
            # enforced with ``asyncio.wait_for``, which is loop-time based and
            # therefore unaffected by the suite's zero-second sleep patch -- the
            # old version needed real sleeps purely to let the five-second
            # default expire, which is a slow way to assert "the cap fired". The
            # resolution path itself is covered by ``harvest_timeout_for``.

            async def call_finally():
                await renderer._render_with_page_unlocked(
                    url,
                    RenderOptions(browser_profile={}),
                    policy=UNRESTRICTED_HOST_POLICY,
                )

            await asyncio.wait_for(call_finally(), timeout=10.0)

            # The harvest cap fired; page close still happened afterwards.
            assert close_calls == [page]
            # No cookies were stored from the wedged context.
            assert browser_mod.get_browser_cookies_for(domain) is None


class TestActionChainFailuresAreVisible:
    """Best-effort is the right policy for an action chain; silence is not.

    Every action error was swallowed by a bare ``except Exception: continue``,
    including the common one -- a ``wait`` for a selector the page never grows.
    That costs extraction quality on exactly the pages an operator most wants to
    hear about (SPA content that never arrived), and it left no evidence at all.

    These drive the REAL ``BrowserDriver``, which is where the chain lives. The
    shared ``build_pinned_renderer`` helper substitutes a driver whose chain is
    a no-op (it is there to drive the render), so a test using it asserts
    against the stub and passes no matter what the production chain does.
    """

    async def test_a_selector_timeout_names_the_selector_url_and_budget(self, caplog):
        driver = BrowserDriver()

        async def never_appears(selector, timeout=None, **kwargs):
            raise TimeoutError(f"waiting for selector {selector!r} timed out after {timeout}ms")

        page = BrowserFakePage(on_wait_for_selector=never_appears)

        with caplog.at_level("WARNING"):
            await driver.run_action_chain(
                page,
                [{"type": "wait", "selector": "#hydration-done", "timeout": 2500}],
                url="http://example.com/page",
                budget_seconds=5.0,
            )

        # The module logs structured records (``extra=``), which is what the
        # JSONL sink emits, so the evidence is in the fields rather than the
        # message.
        failures = [r for r in caplog.records if r.__dict__.get("action") == "wait"]
        assert failures, f"an action failure left no structured record: {caplog.records!r}"
        fields = failures[0].__dict__
        assert fields.get("selector") == "#hydration-done", f"the failing selector is not named: {fields!r}"
        assert fields.get("wait_ms") == 2500, f"the requested wait budget is not named: {fields!r}"
        assert fields.get("url") == "http://example.com/page", f"the URL is not named: {fields!r}"


class TestActionChainIsOneBoundedHold:
    """A pool slot may be held for the render's budget, not for the chain's.

    The page semaphore is held for the whole render, but the only deadline in it
    was the navigation's -- the action chain after it had none, only a
    PER-ACTION clamp. Three ``wait`` actions were therefore 3x10s of pooled
    renderer, on a pool whose whole reason for existing is that concurrent tabs
    crash the shared process, and the exhaustion that follows is reported as
    ``browser_pool_exhausted`` with nothing saying a chain was still waiting.
    One deadline for the chain, taken from the same per-URL budget the
    navigation got, removes the multiplier.
    """

    async def test_a_wedged_chain_releases_the_slot_at_the_render_budget(self, caplog):
        driver = BrowserDriver()
        page = BrowserFakePage()
        observed: list[float | None] = []

        async def never_appears(selector, timeout=None, **kwargs):
            # Records the budget each action was allowed, then wedges: the
            # realistic shape of an SPA that never grows the awaited element.
            observed.append(timeout)
            await asyncio.Event().wait()

        page.on_wait_for_selector = never_appears
        actions = [
            {"type": "wait", "selector": "#one", "timeout": 60_000},
            {"type": "wait", "selector": "#two", "timeout": 60_000},
            {"type": "wait", "selector": "#three", "timeout": 60_000},
        ]
        # 0.05s stands in for the per-URL budget: the deadline is enforced with
        # loop-time-based wait_for, which the suite's sleep patch cannot fake.
        with caplog.at_level("WARNING"):
            await asyncio.wait_for(
                driver.run_action_chain(page, actions, url="http://example.com/page", budget_seconds=0.05),
                timeout=10.0,
            )

        assert len(observed) < len(actions), (
            f"the chain started all {len(actions)} actions inside a 50ms budget: the "
            f"per-action clamps multiplied into a {len(observed)}x10s hold of a pooled "
            "renderer, which is how a pool drains with no log line pointing here"
        )
        exhausted = [r for r in caplog.records if r.__dict__.get("budget_seconds") is not None]
        assert exhausted, (
            "the chain ran out of budget on a pooled slot and said nothing: "
            f"{[r.getMessage() for r in caplog.records]!r}"
        )
        assert exhausted[0].__dict__.get("url") == "http://example.com/page"
