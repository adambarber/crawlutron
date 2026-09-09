"""M3: the cookie harvest must complete before the render returns.

The harvest used to fire-and-forget a collection task, so the cffi retry's
synchronous store read usually raced it and retried cookieless. Awaiting
the harvest makes the browser-minted session visible to the immediate
retry that follows the render.
"""

import asyncio

from app.crawl.strategies import browser as browser_mod
from app.crawl.strategies.browser import BrowserRenderer
from app.validation import registrable_domain


class _FakeContext:
    async def cookies(self, url):
        await asyncio.sleep(0.01)
        return [{"name": "sess", "value": "abc123"}]


class _FakePage:
    def __init__(self):
        self.context = _FakeContext()


class TestCookieHarvestCompletes:
    async def test_harvest_visible_to_immediate_retry(self, real_sleeps):
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
