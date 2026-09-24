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


def pinned_renderer(monkeypatch, *, pinned=("http://1.2.3.4:80/", {"Host": "example.com"})):
    import app.crawl.strategies.browser as browser_mod

    page = BrowserFakePage()
    renderer = build_pinned_renderer(monkeypatch, page)
    monkeypatch.setattr(browser_mod, "_build_pinned_request", lambda url, headers, *, policy: pinned)
    return page, renderer


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
    "pinned_renderer",
]
