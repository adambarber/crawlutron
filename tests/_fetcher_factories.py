import asyncio
import inspect
from typing import Any
from urllib.parse import urlparse

from app.constants import HTTP_DECLARED_CRAWLER
from app.crawl.cache import HttpCache
from app.crawl.content_cache import ContentCache
from app.crawl.engine import crawl_request
from app.models import CrawlResult, HttpFetchResult

SCOPE_KEEP_DROP_PAGES = {
    "/": (
        200,
        '<html><body><a href="/keep/x">k</a><a href="/drop/y">d</a></body></html>',
        "text/html",
    ),
    "/keep/x": (200, "<html>k</html>", "text/html"),
    "/drop/y": (200, "<html>d</html>", "text/html"),
}


def site_fetcher(pages: dict[str, Any]):
    def fetcher(url, headers, timeout, max_content_length=50 * 1024 * 1024):
        path = urlparse(url).path or "/"
        entry = pages.get(path)
        if entry is None:
            return make_fetch(
                status=404,
                body=b"<html></html>",
                ctype="text/html",
                url=url,
                final_url=url,
            )
        status, body, ctype = entry
        if isinstance(body, str):
            body = body.encode("utf-8")
        return make_fetch(status=status, body=body, ctype=ctype, url=url, final_url=url)

    return fetcher


async def run_scoped_crawl(scope_regex=None, *, max_depth=1, max_pages=10, pages=None):
    from app.crawl.cache import HttpCache
    from app.crawl.engine import crawl_request

    pages = pages if pages is not None else SCOPE_KEEP_DROP_PAGES
    payload = {
        "url": "http://example.com/",
        "policy": {"limits": {"max_depth": max_depth, "max_pages": max_pages}, "scope_regex": scope_regex},
    }
    out = await crawl_request(payload, http_fetcher=site_fetcher(pages), cache=HttpCache())
    return {r["normalized_url"] for r in out["results"]}


def make_fetch(
    status=200,
    body=b"<html><body>hello</body></html>",
    ctype="text/html; charset=utf-8",
    final_url="http://example.com/",
    url="http://example.com/",
    headers=None,
    decompression_failed=False,
):
    return HttpFetchResult(
        url=url,
        final_url=final_url,
        status_code=status,
        headers={"content-type": ctype} if headers is None else headers,
        body=body,
        elapsed_ms=1,
        decompression_failed=decompression_failed,
    )


def ok_fetcher(url, headers, timeout, max_content_length=50 * 1024 * 1024):
    return make_fetch(url=url)


def raise_fetcher(url, headers, timeout, max_content_length=50 * 1024 * 1024):
    raise ConnectionError("network down")


def scripted_fetcher(*, status=200, body=b"", headers=None, ctype="text/html; charset=utf-8"):
    """A fetcher that serves one fixed response for whatever URL it is handed.

    Most fetcher doubles in this tree answer every URL the same way, and
    spelling that out meant re-declaring the production fetcher's signature --
    including its real 50 MiB ``max_content_length`` default -- at each site.
    That is the copy the ``scripted-fetcher`` funnel bans: the signature is
    owned by the factory, so a change to the real fetcher lands here once.

    ``headers`` replaces the response headers wholesale (``content-type``
    included), which is what a test about a header or a content-encoding needs;
    otherwise ``ctype`` supplies the content type and nothing else is sent.
    """

    async def fetcher(url, sent_headers, timeout, max_content_length=50 * 1024 * 1024):
        return make_fetch(url=url, final_url=url, status=status, body=body, headers=headers, ctype=ctype)

    return fetcher


def failing_fetcher(error: BaseException | type[BaseException] | None = None, calls: list | None = None):
    """A fetcher that raises instead of responding, optionally recording URLs.

    ``calls`` turns this into a recorder, so a test that asserts the engine
    retried the same URL three times does not need its own closure for it.

    A class is instantiated per call, so each raise is a distinct exception
    with its own traceback; pass an instance only when the exception carries
    state a test needs to inspect (``httpx.HTTPStatusError`` and its response).
    """
    spec = error if error is not None else ConnectionError

    async def fetcher(url, sent_headers, timeout, max_content_length=50 * 1024 * 1024):
        if calls is not None:
            calls.append(url)
        raise spec() if isinstance(spec, type) else spec

    return fetcher


def hanging_fetcher(calls: list | None = None):
    """A fetcher that never returns, so only the caller's deadline ends it.

    The tarpit, for a poisoned URL whose fetch has to time out. It awaits a
    never-resolved future rather than sleeping: the suite monkeypatches
    ``asyncio.sleep`` to return instantly, which would turn the tarpit into a
    fast success and assert nothing.
    """

    async def fetcher(url, sent_headers, timeout, max_content_length=50 * 1024 * 1024):
        if calls is not None:
            calls.append(url)
        await asyncio.Future()

    return fetcher


#: What a route may answer with. An exception is raised, an ``HttpFetchResult``
#: is returned verbatim, a ``(status, body, ctype)`` triple or a kwargs dict is
#: turned into a real ``HttpFetchResult`` by :func:`make_fetch`.
Route = Any


def _answer(spec: Route, url: str):
    if isinstance(spec, BaseException) or (isinstance(spec, type) and issubclass(spec, BaseException)):
        raise spec() if isinstance(spec, type) else spec
    if isinstance(spec, HttpFetchResult):
        return spec
    if isinstance(spec, dict):
        return make_fetch(url=url, final_url=url, **spec)
    status, body, ctype = spec
    if isinstance(body, str):
        body = body.encode("utf-8")
    return make_fetch(url=url, final_url=url, status=status, body=body, ctype=ctype)


def routed_fetcher(
    routes: dict[str, Route] | None = None,
    calls: list | None = None,
    *,
    default: Route = (404, b"<html></html>", "text/html"),
    missing="default",
    on_route=None,
):
    """A fetcher that answers per-URL, by substring match on the URL.

    Almost every hand-rolled fetcher in this tree is the same shape with the
    same three branches -- robots, sitemap, page -- and the branches are what
    makes them worth stating once. ``routes`` maps a URL substring to what that
    fetch should do; the first match wins, so order the specific before the
    general. A route may be an exception (raised), a ``(status, body, ctype)``
    triple, a :func:`make_fetch` kwargs dict, or a prebuilt ``HttpFetchResult``.

    ``calls`` records every URL fetched, which is the assertion most of these
    tests exist to make. ``on_route(url, headers)`` is called when a route
    matches, for a test that counts one *kind* of fetch across fetcher instances
    it does not want to share a fetcher between -- the sharing under test is
    elsewhere, and building a second fetcher per request is the honest way to
    show it.

    ``missing`` is what happens when nothing matches: ``"default"`` serves
    ``default``; ``"raise"`` raises an ``AssertionError`` naming the URL, for a
    test that must not be fetching something it did not script. The default is
    the 404, matching :func:`site_fetcher`, because a crawler test that reads a
    body it did not arrange is a test that passes for the wrong reason.
    """

    async def fetcher(url, sent_headers, timeout, max_content_length=50 * 1024 * 1024):
        if calls is not None:
            calls.append(url)
        for fragment, spec in (routes or {}).items():
            if fragment in url:
                if on_route is not None:
                    on_route(url, sent_headers)
                return _answer(spec, url)
        if missing == "raise":
            raise AssertionError(f"unscripted fetch: {url} (routes: {sorted(routes or {})})")
        return _answer(default, url)

    return fetcher


def sequenced_fetcher(
    steps: list[Route],
    calls: list | None = None,
    *,
    repeat_last: bool = True,
):
    """A fetcher that answers in call order, for a test about what happens next.

    The shape behind "the first strategy gets a CAPTCHA and the escalated one
    gets the page", or "the first attempt drops and the retry recovers". Writing
    it as a closure means an ``if calls == 1`` branch per test and no statement
    of what happens on the third call, which is where these go wrong.

    ``repeat_last`` serves the final step for every call past the list, so a
    test that only cares about the transition still terminates.
    """
    if not steps:
        raise ValueError("sequenced_fetcher needs at least one step")
    state = {"n": 0}

    async def fetcher(url, sent_headers, timeout, max_content_length=50 * 1024 * 1024):
        if calls is not None:
            calls.append(url)
        # The harness owns the position, not the caller's list: a test that
        # never asks for the call log still gets a sequence that advances.
        index = state["n"]
        state["n"] += 1
        if index >= len(steps):
            if not repeat_last:
                raise AssertionError(f"unscripted fetch #{index + 1}: {url}")
            index = len(steps) - 1
        return _answer(steps[index], url)

    fetcher.served = lambda: state["n"]
    return fetcher


def request_fetcher(respond, calls: list | None = None):
    """A fetcher whose answer is computed from ``(url, headers)``.

    The conditional-request seam: what a sitemap or page fetch returns depends
    on the ``If-None-Match`` / ``If-Modified-Since`` header the crawler sent, so
    the branch cannot be keyed on the URL. ``respond(url, headers)`` returns
    whatever :func:`routed_fetcher`'s routes may.

    Distinct from :func:`tap_fetcher` (which delegates to a real fetcher) and
    from :func:`concurrent_fetcher` (which also counts overlap): this one adds
    no scheduling point, so a test about headers does not acquire a
    concurrency behaviour it did not ask for.
    """
    seen = [] if calls is None else calls

    async def fetcher(url, sent_headers, timeout, max_content_length=50 * 1024 * 1024):
        seen.append(dict(sent_headers))
        spec = respond(url, sent_headers)
        if asyncio.iscoroutine(spec):
            spec = await spec
        return _answer(spec, url)

    fetcher.calls = seen
    return fetcher


def tap_fetcher(inner, calls: list | None = None):
    """Wrap another fetcher, recording the URLs that reach it.

    For a test whose subject is a *real* fetcher (the default HTTP fetcher, a
    site fetcher) and whose only extra requirement is to see what was asked
    for. Recording and delegating are separate concerns, so they are separate
    functions: this one owns the list, the wrapped one owns the response.
    """
    if calls is None:
        calls = []
    recorded = calls

    async def fetcher(url, sent_headers, timeout, max_content_length=50 * 1024 * 1024):
        recorded.append(url)
        result = inner(url, sent_headers, timeout, max_content_length)
        # The inner fetcher may be sync (site_fetcher) or async (the real
        # default fetcher); the tap is async either way, because that is the
        # shape the engine awaits.
        if inspect.isawaitable(result):
            result = await result
        return result

    fetcher.calls = recorded
    return fetcher


def concurrent_fetcher(
    respond,
    calls: list | None = None,
    *,
    yield_times: int = 1,
):
    """A fetcher that tracks how many of its calls are in flight at once.

    ``respond(url, headers)`` maps a request to a route spec (see
    :func:`routed_fetcher`) and may be a plain callable or a coroutine function.
    It takes the request for the same reason :func:`request_fetcher` does -- one
    ``respond`` shape across the seam -- and because a fetcher that is deciding
    anything usually needs the headers as much as the URL.

    The counter is raised on entry and lowered on exit, with ``yield_times`` loop
    yields in between: the yield is the scheduling point that makes overlap
    observable at all, and without it an async fetcher that never awaits
    serialises itself and any concurrency assertion passes for free.

    ``peak`` on the returned fetcher is what a concurrency test should assert.
    """
    import asyncio

    state = {"inflight": 0, "peak": 0}
    seen = [] if calls is None else calls

    async def fetcher(url, sent_headers, timeout, max_content_length=50 * 1024 * 1024):
        seen.append(url)
        state["inflight"] += 1
        state["peak"] = max(state["peak"], state["inflight"])
        try:
            for _ in range(yield_times):
                await asyncio.sleep(0)
            spec = respond(url, sent_headers)
            if asyncio.iscoroutine(spec):
                spec = await spec
            return _answer(spec, url)
        finally:
            state["inflight"] -= 1

    fetcher.calls = seen
    fetcher.peak = lambda: state["peak"]
    fetcher.inflight = lambda: state["inflight"]
    return fetcher


def make_crawl_payload(
    url: str,
    *,
    strategies: list[str] | None = None,
    timeout_seconds: int = 10,
    max_retries: int = 0,
    max_discovered_links: int = 50,
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "url": url,
        "policy": {
            "strategies": strategies if strategies is not None else [HTTP_DECLARED_CRAWLER],
            "limits": {
                "timeout_seconds": timeout_seconds,
                "max_retries": max_retries,
                "max_discovered_links": max_discovered_links,
            },
        },
    }
    payload.update(extra)
    return payload


def build_payload(
    *,
    url: str | None = None,
    urls: list[str] | None = None,
    strategies: list[str] | None = None,
    limits: dict | None = None,
    **extra,
) -> dict:
    payload = {}
    if url:
        payload["url"] = url
    if urls:
        payload["urls"] = urls
    if strategies is not None:
        payload["policy"] = {"strategies": strategies}
    if limits is not None:
        payload.setdefault("policy", {})["limits"] = limits
    payload.update(extra)
    return payload


LLM_FORMAT_TEST_HTML = (
    "<html lang='en'><head><title>Test Page</title>"
    "<meta content='no-name-attribute'>"
    "<meta name='description' content=''>"
    "<meta name='description' content='A test page'>"
    "<meta property='og:title' content='OG Title'>"
    "</head><body><main>Content here</main></body></html>"
)


async def crawled_results(
    pages: dict[str, Any],
    *,
    url: str = "http://example.com/",
    limits: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
    spill_path: str | None = None,
    content_cache: Any = None,
    calls: list[str] | None = None,
) -> dict[str, Any]:
    """Run a recursive crawl over scripted ``pages`` and return the API response.

    The one way a test should invoke ``crawl_request`` against a scripted site.
    The payload shape, the fetcher wiring and the cache wiring are stated once
    here, so a policy key or a signature change lands here rather than in each
    recursive test -- and a hand-rolled call cannot quietly differ from a
    routed one, which is the drift this chokepoint exists to prevent.

    ``limits`` is the limits sub-mapping; ``policy`` carries the rest of the
    policy (``discovery``, ``scope_regex``, ``browser_profile``). ``calls``, if
    given, records the URLs that actually reached the network, so a test about
    caching does not have to build its own tap.
    """
    body: dict[str, Any] = {"limits": dict(limits or {}), **(policy or {})}
    fetcher = tap_fetcher(site_fetcher(pages), calls) if calls is not None else site_fetcher(pages)
    return await crawl_request(
        {"url": url, "policy": body},
        http_fetcher=fetcher,
        cache=HttpCache(),
        spill_path=spill_path,
        content_cache=content_cache,
    )


async def crawled_urls(pages: dict[str, Any], **kwargs: Any) -> list[str]:
    """:func:`crawled_results` projected to the crawled URLs, in crawl order.

    Order is crawl order (BFS), which a caller may depend on; a caller that
    only asserts membership wraps this in ``set()``.
    """
    out = await crawled_results(pages, **kwargs)
    return [r["normalized_url"] for r in out["results"]]


async def run_crawl_async(payload: dict, *, cache=None, browser_worker=None) -> dict:
    if cache is None:
        cache = HttpCache()
    return await crawl_request(payload, browser_worker=browser_worker, cache=cache)


async def crawl_homepage_async(
    base_url: str,
    *,
    strategies: list[str] | None = None,
    limits: dict | None = None,
    cache=None,
    browser_worker=None,
) -> dict:
    payload = build_payload(
        url=f"{base_url}/",
        strategies=strategies if strategies is not None else [HTTP_DECLARED_CRAWLER],
        limits=limits
        if limits is not None
        else {"timeout_seconds": 10, "max_retries": 0, "max_discovered_links": 50},
    )
    return await run_crawl_async(payload, cache=cache, browser_worker=browser_worker)


def make_content_cache(tmpdir: str, *, default_ttl: float = 1, clock=None) -> ContentCache:
    if clock is None:
        import time

        clock = time.monotonic
    return ContentCache(tmpdir, default_ttl=default_ttl, clock=clock)


def make_content_cache_with_entry(
    tmpdir: str, key: str = "key1", value: dict = None, *, default_ttl: float = 1, clock=None
) -> ContentCache:
    if value is None:
        value = {"data": "1"}
    cache = make_content_cache(tmpdir, default_ttl=default_ttl, clock=clock)
    cache.set(key, value)
    return cache


def _temp_cache(ttl=1, clock_start=1000.0):
    import tempfile

    from app.crawl.content_cache import ContentCache

    clock_vals = [clock_start]
    return ContentCache(tempfile.mkdtemp(), default_ttl=ttl, clock=lambda: clock_vals[0]), clock_vals


def _spawn_fetch(fn):
    from helpers.server import spawn_server

    server = spawn_server()
    try:
        return fn(server)
    finally:
        server.stop()


async def run_http_strategy(
    *,
    url: str,
    policy: dict[str, Any],
    http_fetcher: Any,
    cache: Any = None,
    content_cache: Any = None,
    **extra: Any,
) -> Any:
    from app.crawl.strategies.http import _http_strategy_result
    from app.validation import HostPolicy

    kwargs: dict[str, Any] = {}
    if cache is not None:
        kwargs["cache"] = cache
    if content_cache is not None:
        kwargs["content_cache"] = content_cache
    return await _http_strategy_result(
        original_url=url,
        normalized_url=url,
        strategy=HTTP_DECLARED_CRAWLER,
        policy=policy,
        http_fetcher=http_fetcher,
        host_policy=HostPolicy.unrestricted(),
        **kwargs,
        **extra,
    )


async def invoke_http_strategy(
    monkeypatch: Any,
    *,
    policy: dict[str, Any],
    resp: Any,
    dedupe_patch: bool = False,
    global_concurrency: int | None = None,
    cap_capture: list[int] | None = None,
) -> Any:
    if dedupe_patch:
        import trafilatura.core

        monkeypatch.setattr(trafilatura.core, "duplicate_test", lambda *a, **k: False)

    async def fetcher(url, headers, timeout, max_content_length=0):
        if cap_capture is not None:
            cap_capture.append(max_content_length)
        return resp(url)

    kwargs: dict[str, Any] = {}
    if global_concurrency is not None:
        kwargs["global_concurrency"] = global_concurrency
    return await run_http_strategy(
        url="http://example.com/",
        policy=policy,
        http_fetcher=fetcher,
        **kwargs,
    )


async def invoke_browser_strategy(
    monkeypatch: Any, *, policy: dict[str, Any], html: str, text: str = "Hello world"
) -> Any:
    from app.crawl.strategies.browser_result import browser_strategy_result
    from app.validation import HostPolicy

    async def browser_worker(job):
        return CrawlResult(
            url=job.url,
            status="success",
            classification="rendered",
            final_url=job.url,
            html=html,
            text=text,
            attempts=1,
            elapsed_ms=5,
            domain="example.com",
            metadata={},
        )

    return await browser_strategy_result(
        original_url="http://example.com/",
        normalized_url="http://example.com/",
        strategy="BROWSER_HEADLESS",
        policy=policy,
        browser_worker=browser_worker,
        host_policy=HostPolicy.unrestricted(),
    )


def make_html_fetcher(calls: list[str], body: bytes = b"<html><body>page content</body></html>"):
    async def fetcher(url, headers, timeout, max_content_length=50 * 1024 * 1024):
        calls.append(url)
        return make_fetch(url=url, final_url=url, status=200, ctype="text/html", body=body)

    return fetcher


async def run_default_http_fetcher(url: str, headers=None):
    from app.crawl.strategies.fetchers import _default_http_fetcher
    from app.validation import HostPolicy

    return await _default_http_fetcher(
        url, {} if headers is None else headers, 30, policy=HostPolicy.unrestricted()
    )


def make_expiring_cache(tmpdir, *, max_total_bytes: int = 10_000):
    now = [1000.0]
    cache = ContentCache(
        tmpdir,
        default_ttl=10,
        clock=lambda: now[0],
        max_entries=100,
        max_total_bytes=max_total_bytes,
    )
    cache.set("a", "x" * 100)
    cache.set("b", "x" * 100)
    return cache, now


__all__ = [
    "LLM_FORMAT_TEST_HTML",
    "SCOPE_KEEP_DROP_PAGES",
    "_spawn_fetch",
    "_temp_cache",
    "build_payload",
    "crawl_homepage_async",
    "invoke_browser_strategy",
    "invoke_http_strategy",
    "make_content_cache",
    "make_content_cache_with_entry",
    "make_crawl_payload",
    "make_expiring_cache",
    "make_fetch",
    "make_html_fetcher",
    "failing_fetcher",
    "concurrent_fetcher",
    "hanging_fetcher",
    "ok_fetcher",
    "raise_fetcher",
    "Route",
    "request_fetcher",
    "routed_fetcher",
    "scripted_fetcher",
    "sequenced_fetcher",
    "tap_fetcher",
    "run_crawl_async",
    "run_default_http_fetcher",
    "run_http_strategy",
    "run_scoped_crawl",
    "site_fetcher",
]
