ROUTE_ARG_UNSET = object()


class BrowserFakePage:
    def __init__(self):
        self.goto_calls = []
        self.route_calls = []
        self.set_extra_http_headers_calls = []
        self.url = "http://1.2.3.4/"

    async def route(self, pattern, handler):
        self.route_calls.append((pattern, handler))

    def on(self, event, handler):
        pass

    async def goto(self, url, wait_until="load", **kwargs):
        if "extra_http_headers" in kwargs:
            raise TypeError("unexpected keyword argument 'extra_http_headers'")
        self.goto_calls.append((url, kwargs))

    async def set_extra_http_headers(self, headers):
        self.set_extra_http_headers_calls.append(headers)

    async def evaluate(self, *a, **k):
        return ""

    async def close(self):
        pass

    async def screenshot(self, **k):
        return b""

    async def query_selector(self, *a, **k):
        return None


class BrowserFakeBrowser:
    def __init__(self, page):
        self._page = page

    async def new_page(self, **kwargs):
        return self._page


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
    from _policy_factories import UNRESTRICTED_HOST_POLICY

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


class FakeRequest:
    def __init__(self, url: str, resource_type: str):
        self.url = url
        self.resource_type = resource_type


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
    from _policy_factories import UNRESTRICTED_HOST_POLICY

    from app.crawl.strategies.browser import _make_ssrf_route_handler

    return _make_ssrf_route_handler(
        resource_mode,
        include_screenshot=include_screenshot,
        policy=UNRESTRICTED_HOST_POLICY,
    )


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

    from _policy_factories import UNRESTRICTED_HOST_POLICY

    from app.crawl.strategies.browser import _make_ssrf_route_handler

    thread_ids: list[int] = []
    monkeypatch.setattr(browser_module, "_build_pinned_request", blocking_pinned_request(thread_ids))
    handler = _make_ssrf_route_handler(policy=UNRESTRICTED_HOST_POLICY)
    route = FakeRoute()
    await handler(route, FakeRequest("http://example.com/", "document"))
    ran_off_loop = bool(thread_ids and thread_ids[0] != threading.main_thread().ident)
    return route, ran_off_loop
