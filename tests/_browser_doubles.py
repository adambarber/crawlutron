from dataclasses import dataclass

ROUTE_ARG_UNSET = object()


class BrowserFakePage:
    """A page double whose ``url`` is a plant the test sets, not a navigation result.

    See :meth:`goto` for why navigation deliberately leaves it alone.
    """

    def __init__(self, url: str = "http://1.2.3.4/"):
        self.goto_calls = []
        self.route_calls = []
        self.set_extra_http_headers_calls = []
        self.url = url

    async def route(self, pattern, handler):
        self.route_calls.append((pattern, handler))

    def on(self, event, handler):
        pass

    async def goto(self, url, wait_until="load", **kwargs):
        # ``url`` is deliberately NOT updated here. On this double it is a
        # *plant* -- the redirect tests set it to the internal host they want
        # the render to be caught redirecting to, and a goto that assigned it
        # would quietly overwrite the plant and let the render pass.
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


#: What the renderer reads out of a page, in the order it asks. A double that
#: answers these is indistinguishable from a loaded page for everything the
#: renderer does; anything beyond this list is a read the test cares about.
DEFAULT_PAGE_ANSWERS = {
    "readyState": "complete",
    "document.title": "Fake Title",
    "innerText": "Visible body text",
    "scrollTo": None,
    "document.body": "Visible body text",
}

DEFAULT_PAGE_HTML = "<html><body><h1>Visible body text</h1></body></html>"


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

    def __init__(self, *, url="http://example.com/", answers=None, html=None, on_goto=None):
        # Everything the base records is the base's to record. Restating the three
        # call lists here was a second answer to "what does this double
        # remember", and one new recorded read had to be added in two places.
        super().__init__(url=url)
        self._answers = dict(DEFAULT_PAGE_ANSWERS if answers is None else answers)
        self._html = DEFAULT_PAGE_HTML if html is None else html
        self._on_goto = on_goto
        self.evaluates: list[str] = []
        self.closed = False

    async def goto(self, url, wait_until="load", **kwargs):
        # Unlike the base, this one *does* move: a scripted page stands in for a
        # page that navigated, and the ``on_goto`` hook may inspect where it
        # landed. The base leaves ``url`` alone because there it is a plant.
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


class BrowserFakeBrowser:
    def __init__(self, page):
        self._page = page

    async def new_page(self, **kwargs):
        return self._page


class BrowserFakeContextManager:
    """The ``async with`` wrapper a browser factory hands back, over one browser."""

    def __init__(self, browser):
        self.browser = browser
        self.enters = 0

    async def __aenter__(self):
        self.enters += 1
        return self.browser

    async def __aexit__(self, *exc):
        return False


async def _browser_noop(self, *a, **k):
    return None


def _async_return(value):
    async def coro():
        return value

    return coro()


def build_pinned_renderer(monkeypatch, page):
    from app.crawl.strategies.browser import BrowserRenderer

    renderer = BrowserRenderer(warmup_urls=[])

    async def fake_acquire(self, options):
        return BrowserFakeBrowser(page)

    monkeypatch.setattr(BrowserRenderer, "_acquire_browser", fake_acquire)
    monkeypatch.setattr(BrowserRenderer, "_get_page_kwargs", lambda self, prof: {})
    monkeypatch.setattr(BrowserRenderer, "_wait_for_ready_state", _browser_noop)
    monkeypatch.setattr(BrowserRenderer, "_run_action_chain", _browser_noop)
    monkeypatch.setattr(BrowserRenderer, "_lazy_load_scroll", _browser_noop)
    monkeypatch.setattr(BrowserRenderer, "_flatten_shadow_dom", _browser_noop)
    monkeypatch.setattr(BrowserRenderer, "_auto_accept_cookie_consent", _browser_noop)
    monkeypatch.setattr(BrowserRenderer, "_safe_close_page", _browser_noop)
    monkeypatch.setattr(
        BrowserRenderer,
        "_extract_with_challenge_wait",
        lambda self, page, options: _async_return(("<html>hi</html>", "hi", "rendered", False)),
    )
    monkeypatch.setattr(BrowserRenderer, "_looks_like_pdf", lambda self, url: False)
    from app.crawl.strategies import browser as browser_module

    monkeypatch.setattr(browser_module, "status_for_classification", lambda c: "success")
    return renderer


#: The address a pinned-render test navigates to: a public IP literal, with the
#: hostname restored as ``Host``. A private address is the one thing the SSRF
#: guard refuses, so a public one is what makes these tests about the pin rather
#: than about the guard. One literal, because every pinned-render arrangement
#: asserting a different address is a test that quietly stopped testing the pin.
PINNED_PUBLIC_IP = ("http://1.2.3.4:80/", {"Host": "example.com"})


def stub_pinned_request(pinned: tuple[str, dict[str, str]] = PINNED_PUBLIC_IP):
    """A stand-in for the off-loop pin entry, shaped like the real binding.

    ``_build_pinned_request_async`` is a binding produced by ``_off_loop``, not a
    ``def``, so an arrangement that replaces it has to supply a coroutine function
    or the call site stops being awaitable. Returning one built here -- rather
    than a ``lambda`` at each site -- is what lets the rendered-URL tests keep
    asserting a fixed pin: the alternative is the real resolver, which decides
    what ``example.com`` happens to point at today and has nothing to do with the
    pin under test.
    """

    async def _stub(url: str, headers: dict[str, str], *, policy):
        return pinned[0], {**headers, **pinned[1]}

    return _stub


def pinned_page_renderer(monkeypatch, page=None, *, pinned=PINNED_PUBLIC_IP):
    """A ``BrowserRenderer`` whose browser hands out `page` and pins every request.

    A plain :class:`BrowserFakePage` unless the test has a reason to want a
    different one -- one that never returns from ``goto``, one that landed on
    loopback -- in which case it passes its own.
    """
    import app.crawl.strategies.browser as browser_mod

    page = BrowserFakePage() if page is None else page
    renderer = build_pinned_renderer(monkeypatch, page)
    monkeypatch.setattr(browser_mod, "_build_pinned_request_async", stub_pinned_request(pinned))
    return page, renderer


async def render_pinned(monkeypatch, url: str = "http://example.com/"):
    """Render `url` through :func:`pinned_page_renderer`; return ``(page, result)``.

    The one navigation a pinned-render test performs, so the two halves of the
    arrangement -- a pinned renderer, and the call into it -- are stated once.
    """
    from tests._policy_factories import UNRESTRICTED_HOST_POLICY

    from app.models import RenderOptions

    page, renderer = pinned_page_renderer(monkeypatch)
    result = await renderer._render_with_browser(
        url,
        RenderOptions(browser_profile={}),
        policy=UNRESTRICTED_HOST_POLICY,
    )
    return page, result


class FakeRoute:
    def __init__(self):
        self.actions = []
        self.aborted = False
        self.continued = False
        self.continued_url = None
        self.continued_headers = ROUTE_ARG_UNSET

    async def abort(self):
        self.aborted = True
        self.actions.append(("abort", {}))

    async def continue_(self, url=ROUTE_ARG_UNSET, headers=ROUTE_ARG_UNSET):
        self.continued = True
        call = {}
        if url is not ROUTE_ARG_UNSET:
            self.continued_url = url
            call["url"] = url
        if headers is not ROUTE_ARG_UNSET:
            self.continued_headers = headers
            call["headers"] = headers
        self.actions.append(("continue", call))

    @property
    def decision(self):
        return self.actions[-1][0] if self.actions else None


@dataclass
class FakeRequest:
    url: str
    resource_type: str


def _fake_browser(html=None, text="doc", metadata=None, status="success", classification="rendered"):
    from app.models import CrawlResult

    def worker(job):
        return CrawlResult(
            url=job.url,
            status=status,
            classification=classification,
            final_url=job.url,
            html=html,
            text=text,
            attempts=1,
            elapsed_ms=1,
            domain="x",
            metadata=metadata or {},
        )

    return worker


__all__ = [
    "BrowserFakeBrowser",
    "BrowserFakePage",
    "FakeRequest",
    "FakeRoute",
    "ROUTE_ARG_UNSET",
    "_fake_browser",
    "build_pinned_renderer",
    "pinned_page_renderer",
    "render_pinned",
    "PINNED_PUBLIC_IP",
    "blocking_pinned_request",
    "response_handler_double",
    "run_blocking_document_route",
    "ssrf_route_handler",
    "assert_route_decision",
]


def ssrf_route_handler(resource_mode=None, *, include_screenshot: bool = False):
    """The production SSRF route handler, over the unrestricted test policy.

    The policy argument is the only difference between this and calling
    ``_make_ssrf_route_handler`` directly, and it is the difference that keeps
    these tests about the blocklist rather than about the guard: a test that
    wants the guard's own verdict builds its own handler and calls it directly.
    """
    from tests._policy_factories import UNRESTRICTED_HOST_POLICY

    from app.crawl.strategies.browser import _make_ssrf_route_handler

    return _make_ssrf_route_handler(
        resource_mode,
        include_screenshot=include_screenshot,
        policy=UNRESTRICTED_HOST_POLICY,
    )


def assert_continued(route, *, url: str | None = None) -> None:
    """The route handler continued the request, at `url` if one is named.

    ``url`` is optional because the two things a continue can be wrong about are
    independent: the decision, and which URL it continued to. Naming the
    expected URL where there is one keeps the pinned-IP hop from being asserted
    by a test that only meant to check the decision.
    """
    assert route.decision == "continue", route.decision
    if url is not None:
        assert route.continued_url == url, route.continued_url


async def assert_route_decision(
    handler,
    urls,
    decision: str,
    *,
    resource_type: str = "document",
) -> None:
    """The handler must reach ``decision`` for every URL in ``urls``.

    A host is on or off a blocklist by SUFFIX, so a tracking host is only proved
    blocked when the apex form, ``www.`` and a deeper subdomain have all been
    asked -- which is why these are lists of URLs rather than single requests.
    The handler is passed in rather than built here so a test that varies the
    mode or the policy says so once, at the call site, instead of the loop
    growing a parameter per variation.
    """
    for url in urls:
        route = FakeRoute()
        await handler(route, FakeRequest(url, resource_type))
        assert route.decision == decision, f"expected {decision} for {url}, got {route.decision}"


async def run_route_with_pinner(monkeypatch, url, *, pinned=None, raises=None, resource_type="document"):
    """Run the SSRF route handler over one request with ``_build_pinned_request`` stubbed.

    The pinner is a seam the handler reaches through ``asyncio.to_thread``, so a
    test about the guard's *verdict* replaces it rather than resolving a real
    host. ``pinned`` is what the stub returns and ``raises`` is what it raises
    instead; supplying neither pins to the request's own URL with no header
    overrides, which is the shape the https/no-Host-override case needs.

    Returns the :class:`FakeRoute` the handler decided on, because the decision,
    the continued URL and the continued headers are what each test is about --
    and because a helper that returned only the verdict would force every caller
    to re-derive the route object to assert the rest.
    """
    import app.crawl.strategies.browser as browser_mod

    if raises is not None:

        def pin(url, headers, *, policy):
            raise raises

    else:
        outcome = pinned if pinned is not None else (url, {})

        def pin(url, headers, *, policy):
            return outcome

    monkeypatch.setattr(browser_mod, "_build_pinned_request", pin)
    route = FakeRoute()
    await ssrf_route_handler()(route, FakeRequest(url, resource_type))
    return route


def scripted_renderer(browser, cm=None, *, sleep=None):
    """A ``BrowserRenderer`` whose browser factory hands back `browser`.

    ``sleep`` is stubbed to a no-op by default because both suites that need a
    renderer are about *what the render orchestrates*, never about how long it
    waited; a suite that genuinely wants the delay passes its own.

    ``cm`` is for the reuse assertions: a test that counts how many times the
    context manager was entered has to be holding the one the factory returns, so
    it passes its own. Left out, a fresh wrapper is made per acquire -- which is
    the right default for a test that does not care.

    Two suites each wrote this lambda, along with the four imports behind it, and
    they agreed by accident. A renderer built with a real sleep made the
    page-cap test take its cap in wall-clock, which is the one measurement it
    explicitly is not.
    """
    from app.crawl.strategies.browser import BrowserRenderer

    return BrowserRenderer(
        browser_factory=lambda **kw: cm or BrowserFakeContextManager(browser),
        sleep=lambda _s: None if sleep is None else sleep(_s),
    )


async def render_bare(renderer, url: str = "http://example.com/"):
    """One render at the empty browser profile over `renderer`, as a raw call.

    ``_render_with_browser`` rather than ``render``: these tests are about what
    the browser half registers and refuses, and the public entry point is the
    thing that decides when the pool is consulted. The empty profile and the
    unrestricted policy are the baseline -- what is under test is what the render
    adds to it, so those two are the same in every one of these calls.
    """
    from tests._policy_factories import UNRESTRICTED_HOST_POLICY

    from app.models import RenderOptions

    return await renderer._render_with_browser(
        url, RenderOptions(browser_profile={}), policy=UNRESTRICTED_HOST_POLICY
    )


def blocking_pinned_request(thread_ids: list[int], delay: float = 0.05):
    import threading
    import time

    def build(url, headers, *, policy):
        thread_ids.append(threading.get_ident())
        time.sleep(delay)
        return PINNED_PUBLIC_IP

    return build


def response_handler_double(url: str, payload=None, error=None):
    from app.crawl.strategies.browser import BrowserRenderer

    collected: list[dict] = []
    handler = BrowserRenderer._make_response_handler(collected)
    response_url = url

    class FakeResponse:
        headers = {"content-type": "application/json"}
        url = response_url
        status = 200

        async def json(self):
            if error is not None:
                raise error
            return payload

    return collected, handler, FakeResponse()


async def run_blocking_document_route(monkeypatch, browser_module):
    import threading

    from tests._policy_factories import UNRESTRICTED_HOST_POLICY

    from app.crawl.strategies.browser import _make_ssrf_route_handler

    thread_ids: list[int] = []
    monkeypatch.setattr(browser_module, "_build_pinned_request", blocking_pinned_request(thread_ids))
    handler = _make_ssrf_route_handler(policy=UNRESTRICTED_HOST_POLICY)
    route = FakeRoute()
    await handler(route, FakeRequest("http://example.com/", "document"))
    ran_off_loop = bool(thread_ids and thread_ids[0] != threading.main_thread().ident)
    return route, ran_off_loop
