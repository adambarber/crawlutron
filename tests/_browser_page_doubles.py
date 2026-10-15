from typing import Any, Protocol

#: What the renderer reads out of a page, in the order it asks.
DEFAULT_PAGE_ANSWERS = {
    "readyState": "complete",
    "document.title": "Fake Title",
    "innerText": "Visible body text",
    "scrollTo": None,
    "document.body": "Visible body text",
}

#: The HTML every factoried page renders, unless a test replaces it.
DEFAULT_PAGE_HTML = "<html><body><h1>Visible body text</h1></body></html>"


class PageCookieContext(Protocol):
    """What the renderer reads off a page's context.

    Production only asks for ``cookies``, so that is the whole contract -- and
    declaring it here is what lets a test's own context double be passed in
    without the page having to name that double's class.
    """

    async def cookies(self, url: str | None = None) -> list[dict[str, str]]: ...


class FakePageContext:
    """A page context that holds no cookies.

    ``harvest_session_cookies`` reads ``page.context.cookies``,
    so a page double with no context is a double the renderer cannot ask -- and
    the two ways tests were getting around that (assigning ``page.context`` after
    construction) put a member on the object that its own type did not declare.
    A context is part of the page's surface, so it is part of the double.
    """

    async def cookies(self, url: str | None = None) -> list[dict[str, str]]:
        return []


async def _selector_is_present(selector, timeout=None, **kwargs):
    """The default ``wait_for_selector``: the awaited element is there.

    The renderer calls this for a page's hydration markers, and a double that
    timed out by default would make every test that happens to use it about the
    wait rather than about whatever it is actually testing.
    """
    return None


class BrowserFakePage:
    """A page double whose ``url`` is a plant the test sets, not a navigation result.

    See :meth:`goto` for why navigation deliberately leaves it alone.

    The surface is the renderer's, not the tests': ``context`` and
    ``wait_for_selector`` are here because the renderer calls them, and a double
    that omitted them made those two reads fail as ``AttributeError`` inside
    production code rather than as a missing member at the call site that
    installed it. ``on_wait_for_selector`` is the hook for a test that needs a
    different answer -- a selector that never appears, or one that wedges the
    wait -- because replacing the method outright is what the missing member
    pushed people into doing.
    """

    def __init__(
        self,
        url: str = "http://1.2.3.4/",
        *,
        context: PageCookieContext | None = None,
        on_wait_for_selector=None,
    ):
        self.goto_calls: list[tuple[str, dict[str, Any]]] = []
        self.route_calls: list[tuple[Any, Any]] = []
        self.set_extra_http_headers_calls: list[dict[str, str]] = []
        self.url = url
        self.context = context if context is not None else FakePageContext()
        self.on_wait_for_selector = on_wait_for_selector or _selector_is_present

    async def route(self, pattern, handler):
        self.route_calls.append((pattern, handler))

    def on(self, event, handler):
        pass

    async def goto(self, url, wait_until="load", **kwargs):
        # ``url`` is deliberately NOT updated here.
        if "extra_http_headers" in kwargs:
            raise TypeError("unexpected keyword argument 'extra_http_headers'")
        self.goto_calls.append((url, kwargs))

    async def set_extra_http_headers(self, headers):
        self.set_extra_http_headers_calls.append(headers)

    async def evaluate(self, *a, **k):
        return ""

    async def content(self):
        return ""

    async def close(self):
        pass

    async def screenshot(self, **k):
        return b""

    async def query_selector(self, *a, **k):
        return None

    async def wait_for_selector(self, selector, timeout=None, **kwargs):
        return await self.on_wait_for_selector(selector, timeout=timeout, **kwargs)


class ScriptedPage(BrowserFakePage):
    """A page that answers the renderer's real reads and records its writes.

    Four browser suites each wrote out the same Playwright surface -- ``route``,
    ``on``, ``goto``, ``set_extra_http_headers``, ``evaluate``, ``content``,
    ``close`` -- and kept one behaviour of their own. The surface is what a
    change to the renderer breaks first, and it broke unevenly: a read the
    renderer newly made raised ``AttributeError`` in one suite and returned
    ``""`` in another, so which tests failed depended on which file the double
    happened to be written in.

    ``answers`` maps a fragment of the evaluated expression to what the page
    reports, so a suite states the reads it cares about rather than re-deriving
    the whole list. ``on_goto`` is the hook for the behaviour that genuinely
    differs -- the page-cap suite counts overlapping tabs on navigation and
    nothing else needs to -- and ``html`` carries the content a test asserts on.
    Every evaluated expression is appended to ``evaluates``, so a test about how
    many times the renderer asks is a length check rather than a counter it has
    to increment itself and keep in agreement.
    """

    def __init__(self, *, url="http://example.com/", answers=None, html=None, on_goto=None, **kwargs):
        # Everything the base records is the base's to record.
        super().__init__(url=url, **kwargs)
        self._answers = dict(DEFAULT_PAGE_ANSWERS if answers is None else answers)
        self._html = DEFAULT_PAGE_HTML if html is None else html
        self._on_goto = on_goto
        self.evaluates: list[str] = []
        self.closed = False

    async def goto(self, url, wait_until="load", **kwargs):
        # Unlike the base, this one *does* move: a scripted page stands in for a page that navigated, and the
        # ``on_goto`` hook may inspect where it landed
        self.url = url
        if self._on_goto is not None:
            await self._on_goto(self)
        return await super().goto(url, wait_until=wait_until, **kwargs)

    async def evaluate(self, expression, *args, **kwargs):
        self.evaluates.append(expression)
        for fragment, value in self._answers.items():
            if fragment in expression:
                return value
        return ""

    async def content(self):
        return self._html

    async def close(self):
        self.closed = True

    @property
    def is_closed(self):
        return self.closed


class BrowserFakeContextManager:
    """The ``async with`` wrapper a browser factory hands back, over one browser."""

    def __init__(self, browser):
        self.browser = browser
        self.enters = 0
        self.exits = 0

    async def __aenter__(self):
        self.enters += 1
        return self.browser

    async def __aexit__(self, *exc):
        self.exits += 1
        return False
