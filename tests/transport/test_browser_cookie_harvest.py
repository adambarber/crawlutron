"""The cookie harvest must complete before the render returns.

The harvest used to fire-and-forget a collection task, so the cffi retry's
sync read raced it and retried cookieless; awaiting it fixes that.
"""

import asyncio
from contextlib import contextmanager

from app.models.normalized_url import NormalizedUrl
from app.transport import browser as browser_mod
from app.transport.browser_cookies import harvest_session_cookies
from app.transport.browser_driver import BrowserDriver
from app.transport.cookie_jar import COOKIE_NAMESPACE
from tests._browser_doubles import BrowserFakePage, FakeBrowserDriver, ScriptedPage, build_pinned_renderer
from tests._policy_factories import UNRESTRICTED_HOST_POLICY, canonical


def _page_with_context():
    """A page whose cookie store answers with one session cookie."""
    return ScriptedPage(context=_FakeContext())


class _FakeContext:
    """A cookie-only context double: the render phases' route install and the
    harvest's cookies read are the only surface this test needs."""

    async def route(self, pattern, handler):
        pass

    def route_web_socket(self, pattern, handler):
        pass

    async def cookies(self, url):
        # A scheduling point, not a duration: the point of the test is that the harvest is AWAITED, so the
        # assertion is on what the synchronous store read sees after the await returns.
        await asyncio.sleep(0)
        return [{"name": "sess", "value": "abc123"}]


@contextmanager
def isolated_browser_cookies(url: NormalizedUrl):
    """Detach the module-global cookie slot for ``url``'s domain, then restore it.

    The store is process-global, the restore was once missing, and no
    per-key removal exists -- so the slot is detached by emptying the store.
    """
    domain = url.site
    browser_mod.reset_process_state()
    try:
        yield domain
    finally:
        browser_mod.reset_process_state()


class TestCookieHarvestCompletes:
    async def test_harvest_visible_to_immediate_retry(self):
        url = canonical("http://harvest-race.test/page")
        with isolated_browser_cookies(url) as domain:
            await harvest_session_cookies(_page_with_context(), url)
            # The synchronous read the cffi retry performs must see cookies NOW.
            assert browser_mod.get_browser_cookies_for(domain) == "sess=abc123"


class TestHarvestIsPartitionedByCookieNamespace:
    """The harvest store must key by the crawl's cookie namespace (#437).

    The persistent jar pins every slot to a caller namespace; the harvest
    store keyed only by domain, leaking one caller's session to the next.
    """

    async def test_harvested_cookies_never_surface_to_another_caller(self):
        url = canonical("http://harvest-leak.test/page")
        with isolated_browser_cookies(url) as domain:
            harvest_token = COOKIE_NAMESPACE.set("caller-a")
            try:
                await harvest_session_cookies(_page_with_context(), url)
            finally:
                COOKIE_NAMESPACE.reset(harvest_token)

            other_token = COOKIE_NAMESPACE.set("caller-b")
            try:
                leaked = browser_mod.get_browser_cookies_for(domain)
            finally:
                COOKIE_NAMESPACE.reset(other_token)

            own_token = COOKIE_NAMESPACE.set("caller-a")
            try:
                own = browser_mod.get_browser_cookies_for(domain)
            finally:
                COOKIE_NAMESPACE.reset(own_token)

        assert leaked is None, f"caller-b's retry saw caller-a's cookies: {leaked!r}"
        assert own == "sess=abc123", "the harvesting caller lost its own session to the partition"


class _WedgedContext:
    """Cookies() that never resolves (simulates a wedged transport); the
    route-install surface is present so the guard wiring succeeds first."""

    # The declared return is what this WOULD answer if it ever did.
    async def cookies(self, url: str | None = None) -> list[dict[str, str]]:
        await asyncio.Event().wait()
        raise AssertionError("unreachable: this coroutine never resolves")

    async def route(self, pattern, handler):
        pass

    def route_web_socket(self, pattern, handler):
        pass


class TestHarvestWaitBounded:
    """A wedged cookies() must not hold the pooled renderer for the
    whole per-URL budget; the finally-block caps the harvest wait."""

    async def test_render_finally_harvest_timeout(self):
        from app.models.render_options import RenderOptions

        url = canonical("http://example.com/page")
        with isolated_browser_cookies(url) as domain:
            page = BrowserFakePage(context=_WedgedContext())
            close_calls: list[bool] = []

            async def record_close(closed_page):
                close_calls.append(closed_page)

            renderer = build_pinned_renderer(
                page,
                driver=FakeBrowserDriver(page, harvest_timeout=0.05, on_close=record_close),
            )
            # The cap is the thing under test, so the driver reports the short deadline rather than the
            # five-second default.

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

    Every action error was swallowed by ``except Exception: continue``,
    including a timed-out ``wait`` -- SPA content that never arrived, no evidence.
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

        # The module logs structured records (``extra=``), which is what the JSONL sink emits, so the evidence
        # is in the fields rather than the message.
        failures = [r for r in caplog.records if r.__dict__.get("action") == "wait"]
        assert failures, f"an action failure left no structured record: {caplog.records!r}"
        fields = failures[0].__dict__
        assert fields.get("selector") == "#hydration-done", f"the failing selector is not named: {fields!r}"
        assert fields.get("wait_ms") == 2500, f"the requested wait budget is not named: {fields!r}"
        assert fields.get("url") == "http://example.com/page", f"the URL is not named: {fields!r}"


class TestActionChainIsOneBoundedHold:
    """A pool slot may be held for the render's budget, not for the chain's.

    The chain once had no deadline, only a per-action clamp, so three
    ``wait`` actions multiplied into a long hold on a shared-process pool.
    """

    async def test_a_wedged_chain_releases_the_slot_at_the_render_budget(self, caplog):
        driver = BrowserDriver()
        page = BrowserFakePage()
        observed: list[float | None] = []

        async def never_appears(selector, timeout=None, **kwargs):
            # Records the budget each action was allowed, then wedges: the realistic shape of an SPA that
            # never grows the awaited element.
            observed.append(timeout)
            await asyncio.Event().wait()

        page.on_wait_for_selector = never_appears
        actions = [
            {"type": "wait", "selector": "#one", "timeout": 60_000},
            {"type": "wait", "selector": "#two", "timeout": 60_000},
            {"type": "wait", "selector": "#three", "timeout": 60_000},
        ]
        # 0.05s stands in for the per-URL budget: the deadline is enforced with loop-time-based wait_for,
        # which the suite's sleep patch cannot fake.
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
