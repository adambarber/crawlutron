from dataclasses import dataclass

from app.models.intercepted_xhr_record import InterceptedXhrRecord
from app.transport.browser import BrowserRenderer
from app.transport.browser_driver import BrowserDriver
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


class FakeBrowserDriver(BrowserDriver):
    """A stand-in for :class:`BrowserDriver` -- a minimal subclass.

    One object instead of the ten private methods once rebound; a missing
    method fails loudly with ``AttributeError``."""

    def __init__(self, page, *, harvest_timeout=0.0, on_close=None):
        # Skip the real BrowserDriver init which launches a browser
        self._browser = None
        self._context = None
        self._pages: list[object] = []
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


def _never_pdf(url):
    return False


def _always_success(classification: str) -> ResultStatus:
    """Every page these renderers fetch classifies as rendered.

    A test about the render rather than the taxonomy would otherwise
    re-derive the classification mapping; a literal is all it needs."""
    del classification
    return "success"


def build_pinned_renderer(page, **overrides):
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

    The real builder is a coroutine produced by ``_off_loop``; returning one
    built here keeps the tests on a fixed pin, not the live resolver."""

    async def _stub(url, headers: dict[str, str], timeout_seconds: float | None = None):
        return pinned[0], {**headers, **pinned[1]}

    return _stub


def pinned_page_renderer(monkeypatch, page=None, *, pinned=PINNED_PUBLIC_IP):
    """A ``BrowserRenderer`` whose browser hands out `page` and pins every request.

    A plain :class:`BrowserFakePage` unless the test has a reason to want a
    different one -- e.g. one never returning from ``goto``."""

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
    result = await renderer.render_once(
        canonical(url),
        RenderOptions(browser_profile={}),
        policy=UNRESTRICTED_HOST_POLICY,
    )
    return page, result


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
    from app.models.result_factory import terminal_attempt

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


def ssrf_route_handler(resource_mode=None, *, include_screenshot: bool = False, pin=None):
    """The production SSRF route handler, over the unrestricted test policy.

    The policy argument is the only difference from calling the factory
    directly, and it keeps these tests about the blocklist, not the guard."""
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

    ``url`` is optional because the decision and the continued URL are
    independent claims."""
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

    A host is on or off a blocklist by SUFFIX, so the apex form, ``www.`` and a
    deeper subdomain are all asked -- hence lists, not single requests."""
    for url in urls:
        route = FakeRoute()
        await handler(route, FakeRequest(url, resource_type))
        assert route.decision == decision, f"expected {decision} for {url}, got {route.decision}"


async def run_route_with_pinner(url, *, pinned=None, raises=None, resource_type="document"):
    """Run the SSRF route handler over one request with a stubbed pin rule.

    ``pinned`` is what the stub returns, ``raises`` what it raises instead;
    returns the :class:`FakeRoute` the handler decided on."""
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

    ``sleep`` is a no-op by default; ``cm`` is for reuse assertions that
    count context-manager entries."""
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

    ``render_once`` rather than ``render``: these tests are about what
    the browser half registers and refuses, not the pooling entry."""
    from app.models.render_options import RenderOptions
    from tests._policy_factories import UNRESTRICTED_HOST_POLICY, canonical

    return await renderer.render_once(
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

    collected: list[InterceptedXhrRecord] = []
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

    The pin records the thread it ran on: an inline resolve stalls every other
    crawl for the length of a DNS lookup."""
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
