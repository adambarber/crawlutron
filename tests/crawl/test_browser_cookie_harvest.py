"""M3: the cookie harvest must complete before the render returns.

The harvest used to fire-and-forget a collection task, so the cffi retry's
synchronous store read usually raced it and retried cookieless. Awaiting
the harvest makes the browser-minted session visible to the immediate
retry that follows the render.
"""

import asyncio

from _policy_factories import UNRESTRICTED_HOST_POLICY

from app.crawl.strategies import browser as browser_mod
from app.crawl.strategies.browser import BrowserRenderer
from app.validation import registrable_domain


class _FakeContext:
    async def cookies(self, url):
        # A scheduling point, not a duration: the point of the test is that the
        # harvest is AWAITED, so the assertion is on what the synchronous store
        # read sees after the await returns.
        await asyncio.sleep(0)
        return [{"name": "sess", "value": "abc123"}]


class _FakePage:
    def __init__(self):
        self.context = _FakeContext()


class TestCookieHarvestCompletes:
    async def test_harvest_visible_to_immediate_retry(self):
        url = "http://harvest-race.test/page"
        domain = registrable_domain(url)
        old = browser_mod._BROWSER_COOKIES_BY_DOMAIN.pop(domain, None)
        try:
            renderer = BrowserRenderer()
            await renderer._harvest_session_cookies(_FakePage(), url)
            # The synchronous read the cffi retry performs must see cookies NOW.
            assert browser_mod.get_browser_cookies_for(domain) == "sess=abc123"
        finally:
            if old is None:
                browser_mod._BROWSER_COOKIES_BY_DOMAIN.pop(domain, None)
            else:
                browser_mod._BROWSER_COOKIES_BY_DOMAIN[domain] = old


class _WedgedContext:
    """N6: cookies() that never resolves (simulates a wedged transport)."""

    async def cookies(self, url):
        await asyncio.Event().wait()


class TestHarvestWaitBounded:
    """N6: a wedged cookies() must not hold the pooled renderer for the
    whole per-URL budget; the finally-block caps the harvest wait."""

    async def test_render_finally_harvest_timeout(self, monkeypatch):
        from _browser_doubles import BrowserFakePage, build_pinned_renderer

        from app.crawl.strategies.browser import BrowserRenderer, RenderOptions

        url = "http://example.com/page"
        domain = registrable_domain(url)
        old = browser_mod._BROWSER_COOKIES_BY_DOMAIN.pop(domain, None)
        try:
            page = BrowserFakePage()
            page.context = _WedgedContext()
            renderer = build_pinned_renderer(monkeypatch, page)
            # The cap is the thing under test, so it is driven to a value the
            # test can afford rather than observed as five elapsed seconds. The
            # deadline is enforced with ``asyncio.wait_for``, which is loop-time
            # based and therefore unaffected by the suite's zero-second sleep
            # patch -- the old version needed real sleeps purely to let the
            # five-second default expire, which is a slow way to assert "the cap
            # fired". The resolution path itself is covered by
            # ``_harvest_timeout_for``.
            monkeypatch.setattr(renderer, "_harvest_timeout_seconds", 0.05)

            close_calls: list[bool] = []

            async def fake_close(self, p):
                close_calls.append(True)

            monkeypatch.setattr(BrowserRenderer, "_safe_close_page", fake_close)

            async def call_finally():
                await renderer._render_with_page_unlocked(
                    url,
                    RenderOptions(browser_profile={}),
                    policy=UNRESTRICTED_HOST_POLICY,
                )

            await asyncio.wait_for(call_finally(), timeout=10.0)

            # The harvest cap fired; page close still happened afterwards.
            assert close_calls == [True]
            # No cookies were stored from the wedged context.
            assert browser_mod.get_browser_cookies_for(domain) is None
        finally:
            if old is None:
                browser_mod._BROWSER_COOKIES_BY_DOMAIN.pop(domain, None)
            else:
                browser_mod._BROWSER_COOKIES_BY_DOMAIN[domain] = old
