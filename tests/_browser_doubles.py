from dataclasses import dataclass

from app.transport.browser import BrowserRenderer
from app.transport.browser_targets import make_response_handler
from app.vocabulary.taxonomy import ResultStatus
from tests._browser_page_doubles import (
    DEFAULT_PAGE_ANSWERS,
    DEFAULT_PAGE_HTML,
    BrowserFakeContextManager,
    BrowserFakePage,
    PageCookieContext,
    ScriptedPage,
)
from tests._browser_pool_doubles import (
    ExhaustedPool,
    FakeBrowserPool,
    NoRendererPool,
    RaisingRenderer,
    RaisingRendererPool,
    owned_browser_pool,
)

ROUTE_ARG_UNSET = object()


__all__ = [
    "ExhaustedPool",
    "FakeBrowserPool",
    "NoRendererPool",
    "RaisingRenderer",
    "RaisingRendererPool",
    "owned_browser_pool",
]


__all__ = [
    "BrowserFakeContextManager",
    "BrowserFakePage",
    "DEFAULT_PAGE_ANSWERS",
    "DEFAULT_PAGE_HTML",
    "ExhaustedPool",
    "FakeBrowserPool",
    "NoRendererPool",
    "PageCookieContext",
    "RaisingRenderer",
    "RaisingRendererPool",
    "ScriptedPage",
    "owned_browser_pool",
]


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


#: The address a pinned-render test navigates to: a public IP literal, with the hostname restored as ``Host``.
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

    async def _stub(url, headers: dict[str, str]):
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
        self.continued_url = None
        self.continued_headers = ROUTE_ARG_UNSET

    async def abort(self):
        self.actions.append(("abort", {}))

    async def continue_(self, url=ROUTE_ARG_UNSET, headers=ROUTE_ARG_UNSET):
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
    from app.models.crawl_result import CrawlResult
    from app.orchestration.render_result import terminal_attempt

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
    from app.transport.browser import make_ssrf_route_handler
    from app.transport.pinned_transport import build_pinned_request
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

        def pin(url, headers):
            raise raises

    else:
        outcome = pinned if pinned is not None else (url, {})

        def pin(url, headers):
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
    from app.transport.browser_driver import BrowserDriver

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

    def build(url, headers):
        thread_ids.append(threading.get_ident())
        time.sleep(delay)
        return PINNED_PUBLIC_IP

    return build


def response_handler_double(url: str, payload=None, error=None):

    collected: list[dict] = []
    handler = make_response_handler(collected)
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

    from app.transport.browser import make_ssrf_route_handler
    from tests._policy_factories import UNRESTRICTED_HOST_POLICY

    thread_ids: list[int] = []
    handler = make_ssrf_route_handler(
        policy=UNRESTRICTED_HOST_POLICY, pin=blocking_pinned_request(thread_ids)
    )
    route = FakeRoute()
    await handler(route, FakeRequest("http://example.com/", "document"))
    ran_off_loop = bool(thread_ids and thread_ids[0] != threading.main_thread().ident)
    return route, ran_off_loop
