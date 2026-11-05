from collections.abc import Callable

from crawlutron.transport.render_page import GuardContext

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


# The double satisfies the production GuardContext Protocol directly; redeclaring
# the same shape here would be a second hand-rolled contract that can drift.
PageCookieContext = GuardContext


class FakePageContext:
    """A page context that holds no cookies and records route installs: the
    render phases install their SSRF guards on ``page.context.route``."""

    def __init__(self) -> None:
        self.route_calls: list[tuple[str, object]] = []
        self.ws_calls: list[tuple[str, object]] = []

    async def cookies(self, urls: str | list[str] | None = None) -> list[dict[str, str]]:
        return []

    async def route(self, pattern: str, handler: Callable[..., object]) -> object:
        self.route_calls.append((pattern, handler))
        return None

    def route_web_socket(self, pattern: str, handler: Callable[..., object]) -> object:
        self.ws_calls.append((pattern, handler))
        return None


async def _selector_is_present(selector, timeout=None, **kwargs):
    """The default ``wait_for_selector``: the awaited element is there, so a double
    that timed out by default would make every hydration test about the wait rather
    than its actual subject.
    """
    return None


class BrowserFakePage:
    """A page double whose ``url`` is the test's plant, not a navigation result; the
    surface is the renderer's, and omitting it fired ``AttributeError`` inside
    production -- ``on_wait_for_selector`` is the hook for a different answer.
    """

    def __init__(
        self,
        url: str = "http://1.2.3.4/",
        *,
        context: PageCookieContext | None = None,
        on_wait_for_selector=None,
    ):
        self.goto_calls: list[tuple[str, dict[str, object]]] = []
        self.set_extra_http_headers_calls: list[dict[str, str]] = []
        self.url = url
        self.context = context if context is not None else FakePageContext()
        self.on_wait_for_selector = on_wait_for_selector or _selector_is_present

    async def on(self, event, handler):
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

    async def hover(self, selector: str) -> object:
        return None

    async def fill(self, selector: str, value: str) -> object:
        return None

    async def click(self, selector: str) -> object:
        return None


class _ContextSwappingPage(BrowserFakePage):
    """A page double that swaps in a subclass-supplied context class by default."""

    context_cls: type = FakePageContext

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if kwargs.get("context") is None:
            self.context = self.context_cls()


class ScriptedPage(BrowserFakePage):
    """A page that answers the renderer's real reads and records its writes; ``answers``
    maps an expression fragment to what the page reports, ``on_goto`` is the hook for
    behaviour that differs, and every ``evaluate`` lands in ``evaluates``.
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
