from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Protocol

from app.constants import ResultStatus
from app.crawl.strategies.browser import BrowserRenderer
from app.crawl.strategies.browser_pool import (
    BROWSER_POOL_ACQUIRE_TIMEOUT_SECONDS,
    BrowserPool,
    set_browser_pool,
)

ROUTE_ARG_UNSET = object()


@contextmanager
def owned_browser_pool(pool: BrowserPool | None) -> Iterator[None]:
    """Install ``pool`` as the process-wide browser pool, then restore the previous one.

    The save/restore lives here because a test that installs a pool and does not
    restore it makes every later test's 503-versus-200 answer depend on test
    ordering -- and the failure lands somewhere else entirely. Written once
    because the alternative is two copies that drift: the one that grows a
    ``try/finally`` protects its neighbours and the one that does not does not,
    and which is which is only visible in the diff.

    ``None`` installs the no-pool state, which is how a test asserts the pool-down
    guard answers 503.
    """
    previous = set_browser_pool(pool)
    try:
        yield
    finally:
        set_browser_pool(previous)


class FakeBrowserPool(BrowserPool):
    """A browser pool that satisfies the engine's guard and refuses to render.

    Subclasses ``BrowserPool`` rather than duck-typing it, so ``set_browser_pool``
    is checked against the class it actually stores: a stub that is not a pool
    can be handed to code calling any method on it, and the signature is the only
    thing that notices. ``BrowserPool.__init__`` does no I/O -- it only builds the
    queue and lock -- so a real instance is as cheap as a stub.

    ``acquire`` raises rather than returning a renderer. The tests that install a
    pool do it to stop the engine 503-ing with ``browser_pool_unavailable``, and a
    browser that renders during a hermetic test is a failure, not a slower test.
    """

    def __init__(self) -> None:
        super().__init__(pool_size=1)
        self.closed = False

    async def acquire(self, timeout: float | None = BROWSER_POOL_ACQUIRE_TIMEOUT_SECONDS) -> BrowserRenderer:
        raise AssertionError("a test that installs a fake pool must never acquire a renderer")

    async def close(self) -> None:
        self.closed = True
        await super().close()


class PageCookieContext(Protocol):
    """What the renderer reads off a page's context.

    Production only asks for ``cookies``, so that is the whole contract -- and
    declaring it here is what lets a test's own context double be passed in
    without the page having to name that double's class.
    """

    async def cookies(self, url: str | None = None) -> list[dict[str, str]]: ...


class FakePageContext:
    """A page context that holds no cookies.

    ``BrowserRenderer._harvest_session_cookies`` reads ``page.context.cookies``,
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

    async def wait_for_selector(self, selector, timeout=None, **kwargs):
        return await self.on_wait_for_selector(selector, timeout=timeout, **kwargs)


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

    def __init__(self, *, url="http://example.com/", answers=None, html=None, on_goto=None, **kwargs):
        # Everything the base records is the base's to record. Restating the three
        # call lists here was a second answer to "what does this double
        # remember", and one new recorded read had to be added in two places.
        super().__init__(url=url, **kwargs)
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


class FakeBrowserDriver:
    """A stand-in for :class:`BrowserDriver` -- a duck type, NOT a subclass.

    Substituted as one object instead of the ten private ``BrowserRenderer``
    methods this used to need rebound: the driver is the boundary the renderer
    was missing, so a test that wants the renderer's orchestration against a
    non-browser page now hands it a driver and says nothing else.

    ``open_page`` returns the page it was constructed with -- there is no fake
    *browser* any more, because nothing above the driver ever saw one.

    It is deliberately a hand-written duck type rather than a subclass, and the
    docstring used to call it "a real ``BrowserDriver`` collaborator", which is
    the one description it is not: the renderer's parameter is annotated
    ``BrowserDriver | None`` and this is not a ``BrowserDriver``, so the
    annotation is a lie the type checker cannot see (mypy does not cover
    ``tests/``). Anything the renderer calls that this does not implement fails
    loudly with ``AttributeError``, which is the intended trade: a missing method
    on the double must not read as a passing test.
    """

    def __init__(self, page, *, harvest_timeout=0.0, on_close=None):
        self.page = page
        self.harvest_timeout = harvest_timeout
        self.on_close = on_close
        self.opened: list[str | None] = []
        self.closed = 0

    async def open_page(self, browser_profile):
        self.opened.append(browser_profile)
        return self.page

    async def close(self):
        self.closed += 1

    def note_render(self):
        return None

    @property
    def reused(self):
        return False

    def harvest_timeout_for(self, browser_profile):
        return self.harvest_timeout

    async def wait_for_ready_state(self, page):
        return None

    async def run_action_chain(self, page, actions, *, url="", budget_seconds=None):
        return None

    async def lazy_load_scroll(self, page, browser_profile=None):
        return None

    async def flatten_shadow_dom(self, page):
        return None

    async def auto_accept_cookie_consent(self, page, enabled):
        return None

    async def safe_close_page(self, page):
        if self.on_close is not None:
            await self.on_close(page)

    async def capture_element_screenshots(self, page, selectors):
        return []


async def _static_extraction(page, options):
    return ("<html>hi</html>", "hi", "rendered", False)


async def _never_pdf(url):
    return False


def _always_success(classification: str) -> ResultStatus:
    """Every page these renderers fetch classifies as rendered.

    The real resolver maps a classification to the status the result carries; a
    test about the render rather than the taxonomy would otherwise re-derive that
    mapping, and a literal is the whole of what it needs.
    """
    del classification
    return "success"


def build_pinned_renderer(page, monkeypatch=None, **overrides):
    """A renderer whose page work is a fake driver and whose output is fixed.

    ``overrides`` steer the two renderer-owned collaborators a test varies:
    ``extract=`` and ``looks_like_pdf=``.
    """
    from app.crawl.strategies.browser import BrowserRenderer

    renderer = BrowserRenderer(
        warmup_urls=[],
        # a driver the caller may have supplied its own; otherwise the default
        driver=overrides.pop("driver", None) or FakeBrowserDriver(page),
        extract=overrides.pop("extract", _static_extraction),
        looks_like_pdf=overrides.pop("looks_like_pdf", _never_pdf),
        status_for=overrides.pop("status_for", _always_success),
        **overrides,
    )
    return renderer


#: The address a pinned-render test navigates to: a public IP literal, with the
#: hostname restored as ``Host``. A private address is the one thing the SSRF
#: guard refuses, so a public one is what makes these tests about the pin rather
#: than about the guard. One literal, because every pinned-render arrangement
#: asserting a different address is a test that quietly stopped testing the pin.
PINNED_PUBLIC_IP = ("http://1.2.3.4:80/", {"Host": "example.com"})


def stub_pinned_request(pinned: tuple[str, dict[str, str]] = PINNED_PUBLIC_IP):
    """A stand-in for the off-loop pin entry, shaped like the real binding.

    ``build_pinned_request_async`` is a binding produced by ``_off_loop``, not a
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

    page = BrowserFakePage() if page is None else page
    renderer = build_pinned_renderer(page, pin=stub_pinned_request(pinned))
    return page, renderer


async def render_pinned(monkeypatch, url: str = "http://example.com/"):
    """Render `url` through :func:`pinned_page_renderer`; return ``(page, result)``.

    The one navigation a pinned-render test performs, so the two halves of the
    arrangement -- a pinned renderer, and the call into it -- are stated once.
    """
    from app.models.render_options import RenderOptions
    from tests._policy_factories import UNRESTRICTED_HOST_POLICY, canonical

    page, renderer = pinned_page_renderer(monkeypatch)
    result = await renderer._render_with_browser(
        canonical(url),
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
    from app.crawl.result_helpers import terminal_attempt
    from app.models.crawl_result import CrawlResult

    def worker(job):
        return CrawlResult(
            url=job.url,
            attempt=terminal_attempt(
                strategy=None,
                status=status,
                classification=classification,
                elapsed_ms=1,
                error=None,
            ),
            final_url=job.url,
            html=html,
            text=text,
            attempts=1,
            domain=job.url.site,
            metadata=metadata or {},
        )

    return worker


__all__ = [
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


def ssrf_route_handler(resource_mode=None, *, include_screenshot: bool = False, pin=None):
    """The production SSRF route handler, over the unrestricted test policy.

    The policy argument is the only difference between this and calling
    ``make_ssrf_route_handler`` directly, and it is the difference that keeps
    these tests about the blocklist rather than about the guard: a test that
    wants the guard's own verdict builds its own handler and calls it directly.
    """
    from app.crawl.strategies.browser import make_ssrf_route_handler
    from app.crawl.strategies.pinned_transport import build_pinned_request
    from tests._policy_factories import UNRESTRICTED_HOST_POLICY

    return make_ssrf_route_handler(
        resource_mode,
        include_screenshot=include_screenshot,
        policy=UNRESTRICTED_HOST_POLICY,
        pin=pin or build_pinned_request,
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


async def run_route_with_pinner(url, *, pinned=None, raises=None, resource_type="document"):
    """Run the SSRF route handler over one request with a stubbed pin rule.

    The pinner is a seam the handler reaches through ``asyncio.to_thread``, so a
    test about the guard's *verdict* substitutes it rather than resolving a real
    host. ``pinned`` is what the stub returns and ``raises`` is what it raises
    instead; supplying neither pins to the request's own URL with no header
    overrides, which is the shape the https/no-Host-override case needs.

    Returns the :class:`FakeRoute` the handler decided on, because the decision,
    the continued URL and the continued headers are what each test is about --
    and because a helper that returned only the verdict would force every caller
    to re-derive the route object to assert the rest.
    """
    if raises is not None:

        def pin(url, headers, *, policy):
            raise raises

    else:
        outcome = pinned if pinned is not None else (url, {})

        def pin(url, headers, *, policy):
            return outcome

    route = FakeRoute()
    await ssrf_route_handler(pin=pin)(route, FakeRequest(url, resource_type))
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
    from app.crawl.strategies.browser_driver import BrowserDriver

    def _sleep(_seconds):
        return None if sleep is None else sleep(_seconds)

    return BrowserRenderer(
        driver=BrowserDriver(
            browser_factory=lambda **kw: cm or BrowserFakeContextManager(browser),
            sleep=_sleep,
        ),
        sleep=_sleep,
    )


async def render_bare(renderer, url: str = "http://example.com/"):
    """One render at the empty browser profile over `renderer`, as a raw call.

    ``_render_with_browser`` rather than ``render``: these tests are about what
    the browser half registers and refuses, and the public entry point is the
    thing that decides when the pool is consulted. The empty profile and the
    unrestricted policy are the baseline -- what is under test is what the render
    adds to it, so those two are the same in every one of these calls.
    """
    from app.models.render_options import RenderOptions
    from tests._policy_factories import UNRESTRICTED_HOST_POLICY, canonical

    return await renderer._render_with_browser(
        canonical(url), RenderOptions(browser_profile={}), policy=UNRESTRICTED_HOST_POLICY
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
    handler = BrowserRenderer.make_response_handler(collected)
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


async def run_blocking_document_route():
    """Drive one document route through the handler and report the resolver thread.

    The pin records the thread it ran on, which is the property: a synchronous
    resolve that runs inline stalls every other crawl in the process for the length
    of a DNS lookup.
    """
    import threading

    from app.crawl.strategies.browser import make_ssrf_route_handler
    from tests._policy_factories import UNRESTRICTED_HOST_POLICY

    thread_ids: list[int] = []
    handler = make_ssrf_route_handler(
        policy=UNRESTRICTED_HOST_POLICY, pin=blocking_pinned_request(thread_ids)
    )
    route = FakeRoute()
    await handler(route, FakeRequest("http://example.com/", "document"))
    ran_off_loop = bool(thread_ids and thread_ids[0] != threading.main_thread().ident)
    return route, ran_off_loop
