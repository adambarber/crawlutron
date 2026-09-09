"""M4: cookie-consent auto-accept is explicit opt-in, never implicit.

Clicking "Accept All" on a caller's behalf without consent contradicts the
service's no-click posture and has legal weight. The behavior is gated
behind browser_profile.auto_accept_consent (default off).
"""

from _testkit import BrowserFakePage

from app.crawl.strategies.browser import BrowserRenderer


class _Clickable:
    def __init__(self):
        self.clicks = 0

    async def is_visible(self):
        return True

    async def click(self):
        self.clicks += 1


class _ConsentPage(BrowserFakePage):
    def __init__(self):
        super().__init__()
        self.element = _Clickable()

    async def query_selector(self, selector):
        return self.element

    async def evaluate(self, *a, **k):
        return False


async def test_consent_clicking_off_by_default():
    page = _ConsentPage()
    renderer = BrowserRenderer()
    assert await renderer._auto_accept_cookie_consent(page, False) is False
    assert page.element.clicks == 0


async def test_consent_clicking_when_opted_in():
    page = _ConsentPage()
    renderer = BrowserRenderer()
    assert await renderer._auto_accept_cookie_consent(page, True) is True
    assert page.element.clicks == 1
