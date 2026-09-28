import asyncio
import inspect
from typing import Any
from urllib.parse import urlparse

from app.constants import DEFAULT_MAX_CONTENT_LENGTH_BYTES, HTTP_DECLARED_CRAWLER
from app.crawl.cache import HttpCache
from app.crawl.content_cache import ContentCache
from app.crawl.engine import crawl_request
from app.crawl.policy import CrawlPolicy, read_policy
from app.crawl.strategies.strategy_request import StrategyRequest
from app.models import HttpFetchResult
from app.validation import HostPolicy
from tests._browser_doubles import _fake_browser
from tests._policy_factories import UNRESTRICTED_HOST_POLICY

SCOPE_KEEP_DROP_PAGES = {
    "/": (
        200,
        '<html><body><a href="/keep/x">k</a><a href="/drop/y">d</a></body></html>',
        "text/html",
    ),
    "/keep/x": (200, "<html>k</html>", "text/html"),
    "/drop/y": (200, "<html>d</html>", "text/html"),
}


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


def fetcher_double(
    answer,
    calls: list | None = None,
    *,
    record_headers: list | None = None,
    record_cap: list | None = None,
    answer_is_response: bool = False,
):
    """A fetcher double: the production contract, the request seam, the answer.

    Every double in this module used to re-state the fetcher signature, cap
    default included -- and the cap they stated (50 MiB) is not the one
    production declares (``DEFAULT_MAX_CONTENT_LENGTH_BYTES``, 10 MiB), so each
    copy asserted a contract the crawler does not have. One signature, against
    production's own constant, is what makes a double a double instead of a
    transcription.

    ``answer(url, sent_headers, timeout, max_content_length)`` receives the whole
    request, so a double that delegates (see :func:`tap_fetcher`) forwards it
    untouched, and its return is read as a :data:`Route` unless
    ``answer_is_response`` says the answer already *is* the response. The three
    ``record_*`` lists are the observations a crawler test makes -- which URLs
    were fetched, which headers were sent, which body cap the strategy applied
    -- so none of them needs a second fetcher or a hand-rolled wrapper.
    """

    async def fetcher(url, sent_headers, timeout, max_content_length=DEFAULT_MAX_CONTENT_LENGTH_BYTES):
        if calls is not None:
            calls.append(url)
        if record_headers is not None:
            record_headers.append(dict(sent_headers))
        if record_cap is not None:
            record_cap.append(max_content_length)
        result = answer(url, sent_headers, timeout, max_content_length)
        if inspect.isawaitable(result):
            result = await result
        return result if answer_is_response else _answer(result, url)

    fetcher.calls = [] if calls is None else calls
    return fetcher


def site_fetcher(pages: dict[str, Any]):
    """A fetcher serving a scripted page table by path, 404 for anything else.

    A crawler test that reads a body it did not arrange passes for the wrong
    reason, so an unscripted path is a 404 rather than a default page.
    """

    def answer(url, *_):
        entry = pages.get(urlparse(url).path or "/")
        if entry is None:
            return {"status": 404, "body": b"<html></html>", "ctype": "text/html"}
        status, body, ctype = entry
        return {
            "status": status,
            "body": body.encode("utf-8") if isinstance(body, str) else body,
            "ctype": ctype,
        }

    return fetcher_double(answer)


async def run_scoped_crawl(scope_regex=None, *, max_depth=1, max_pages=10, pages=None):
    pages = pages if pages is not None else SCOPE_KEEP_DROP_PAGES
    payload = {
        "url": "http://example.com/",
        "policy": {"limits": {"max_depth": max_depth, "max_pages": max_pages}, "scope_regex": scope_regex},
    }
    out = await crawl_request(payload, http_fetcher=site_fetcher(pages), cache=HttpCache())
    return {r["normalized_url"] for r in out["results"]}


def ok_fetcher(url, sent_headers, timeout, max_content_length=DEFAULT_MAX_CONTENT_LENGTH_BYTES):
    """The 200-for-everything fetcher, passed around as a value.

    Callers hand this function itself to the engine, so it is a fetcher rather
    than a factory; the cap still comes from production's constant.
    """
    return make_fetch(url=url)


def raise_fetcher(url, sent_headers, timeout, max_content_length=DEFAULT_MAX_CONTENT_LENGTH_BYTES):
    """The network-is-down fetcher, passed around as a value like ``ok_fetcher``."""
    raise ConnectionError("network down")


def scripted_fetcher(*, status=200, body=b"", headers=None, ctype="text/html; charset=utf-8"):
    """A fetcher that serves one fixed response for whatever URL it is handed.

    Most fetcher doubles in this tree answer every URL the same way, and
    spelling that out meant re-stating the production fetcher's signature at
    each site. That is the copy the ``scripted-fetcher`` funnel bans: the
    signature, and the cap's default, are owned by :func:`fetcher_double`, so
    a change to the real fetcher lands here once.

    ``headers`` replaces the response headers wholesale (``content-type``
    included), which is what a test about a header or a content-encoding needs;
    otherwise ``ctype`` supplies the content type and nothing else is sent.
    """
    return fetcher_double(
        lambda url, *_: {"status": status, "body": body, "headers": headers, "ctype": ctype}
    )


def failing_fetcher(error: BaseException | type[BaseException] | None = None, calls: list | None = None):
    """A fetcher that raises instead of responding, optionally recording URLs.

    ``calls`` turns this into a recorder, so a test that asserts the engine
    retried the same URL three times does not need its own closure for it.

    A class is instantiated per call, so each raise is a distinct exception
    with its own traceback; pass an instance only when the exception carries
    state a test needs to inspect (``httpx.HTTPStatusError`` and its response).
    """
    return fetcher_double(lambda url, *_: error if error is not None else ConnectionError, calls)


def hanging_fetcher(calls: list | None = None):
    """A fetcher that never returns, so only the caller's deadline ends it.

    The tarpit, for a poisoned URL whose fetch has to time out. It awaits a
    never-resolved future rather than sleeping: the suite monkeypatches
    ``asyncio.sleep`` to return instantly, which would turn the tarpit into a
    fast success and assert nothing.
    """

    async def never(url, *_):
        await asyncio.Future()

    return fetcher_double(never, calls)


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

    def answer(url, sent_headers, *_):
        for fragment, spec in (routes or {}).items():
            if fragment in url:
                if on_route is not None:
                    on_route(url, sent_headers)
                return spec
        if missing == "raise":
            raise AssertionError(f"unscripted fetch: {url} (routes: {sorted(routes or {})})")
        return default

    return fetcher_double(answer, calls)


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

    def answer(url, *_):
        # The harness owns the position, not the caller's list: a test that
        # never asks for the call log still gets a sequence that advances.
        index = state["n"]
        state["n"] += 1
        if index >= len(steps):
            if not repeat_last:
                raise AssertionError(f"unscripted fetch #{index + 1}: {url}")
            index = len(steps) - 1
        return steps[index]

    fetcher = fetcher_double(answer, calls)
    fetcher.served = lambda: state["n"]
    return fetcher


def request_fetcher(respond, calls: list | None = None):
    """A fetcher whose answer is computed from ``(url, headers)``.

    The conditional-request seam: what a sitemap or page fetch returns depends
    on the ``If-None-Match`` / ``If-Modified-Since`` header the crawler sent, so
    the branch cannot be keyed on the URL. ``respond(url, headers)`` -- the
    request, not the whole contract -- returns whatever :func:`routed_fetcher`'s
    routes may, or a finished response when ``answer_is_response`` is set.

    Distinct from :func:`tap_fetcher` (which delegates to a real fetcher) and
    from :func:`concurrent_fetcher` (which also counts overlap): this one adds
    no scheduling point, so a test about headers does not acquire a
    concurrency behaviour it did not ask for. ``fetcher.calls`` is the header
    log, which is what this seam is for.
    """
    seen = [] if calls is None else calls
    fetcher = fetcher_double(lambda url, sent_headers, *_: respond(url, sent_headers), record_headers=seen)
    fetcher.calls = seen
    return fetcher


def tap_fetcher(inner, calls: list | None = None):
    """Wrap another fetcher, recording the URLs that reach it.

    For a test whose subject is a *real* fetcher (the default HTTP fetcher, a
    site fetcher) and whose only extra requirement is to see what was asked
    for. Recording and delegating are separate concerns, so they are separate
    functions: this one owns the list, the wrapped one owns the response --
    which may be sync or async, since :func:`fetcher_double` awaits either.
    """
    return fetcher_double(
        lambda url, sent_headers, timeout, cap: inner(url, sent_headers, timeout, cap), calls
    )


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
    state = {"inflight": 0, "peak": 0}

    async def answer(url, sent_headers, *_):
        state["inflight"] += 1
        state["peak"] = max(state["peak"], state["inflight"])
        try:
            for _ in range(yield_times):
                await asyncio.sleep(0)
            return respond(url, sent_headers)
        finally:
            state["inflight"] -= 1

    fetcher = fetcher_double(answer, calls)
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


def xml_page(body: str) -> tuple[int, str, str]:
    """One ``application/xml`` page, in the shape a page map wants.

    The content type is the part that carries meaning, and it is the part a
    sitemap fixture leaves out: the crawl only reads a served document as a
    sitemap when the type says xml, so a sitemap fixture without it exercises the
    in-page-link fallback instead and passes while the sitemap path is broken.
    """
    return (200, body, "application/xml")


# Deep enough to follow the seed's links, wide enough that the page cap is never
# what a test is measuring. Named because several recursive tests stated the same
# two numbers, and a test that meant to change the cap could not tell which of
# them it was editing.
LINKED_PAIR_LIMITS = {"max_depth": 1, "max_pages": 10}


async def crawled_results(
    pages: dict[str, Any],
    *,
    url: str = "http://example.com/",
    limits: dict[str, Any] | None = None,
    policy: CrawlPolicy | dict[str, Any] | None = None,
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


def site_linking_to(*paths: str, bodies: dict[str, str] | None = None) -> dict[str, Any]:
    """A fake site: an index linking to each of ``paths``, plus a page per path.

    The page map :func:`crawled_results` and :func:`crawled_urls` are driven
    over. A test names the children it wants and gets the map; ``bodies`` is
    there for the tests whose point is a child's *content* (the memory-strip
    cases need a page with real text in it, not the one-word default). Nothing
    here decides anything a crawl decides: depth, page cap, scope and policy
    stay with the caller, because those are what those tests are about.

    The default child body is the path's own name (``/a`` -> ``<html>a</html>``),
    which keeps the map readable in an assertion diff.
    """
    bodies = bodies or {}
    links = "".join(f'<a href="{path}">{path.strip("/") or path}</a>' for path in paths)
    return {
        "/": (200, f"<html><body>{links}</body></html>", "text/html"),
        **{path: (200, bodies.get(path, f"<html>{path.strip('/')}</html>"), "text/html") for path in paths},
    }


def cache_with_etag(url: str, etag: str = "v1") -> HttpCache:
    """An HTTP cache that already holds a validator for ``url``.

    A conditional request is only conditional if something was stored, so the
    304 path is unreachable until a test says what was cached. Stating that as
    "the cache holds an etag for this URL" keeps the validator, the URL it is
    keyed by and the cache object one decision instead of three.
    """
    cache = HttpCache()
    cache.store(url, {"etag": etag})
    return cache


async def crawled_result(
    url: str,
    *,
    policy: CrawlPolicy | dict[str, Any] | None = None,
    strategies: list[str] | None = None,
    http_fetcher: Any = None,
    browser_worker: Any = None,
    cache: Any = None,
    content_cache: Any = None,
    **payload_kwargs: Any,
) -> dict[str, Any]:
    """The one result a real ``crawl_request`` returns for ``url``.

    The single-URL sibling of :func:`crawled_results`, and the same argument:
    "what came back for this URL?" is a payload, a fetcher and cache wiring, and
    a ``results[0]`` projection, and every site that asked used to spell all
    three. A hand-rolled call can differ from a routed one in a way nothing
    notices -- a dropped cache, a payload key the engine never read -- and the
    copy is the one the test actually ran.

    ``policy`` is the whole policy when the test has a reason to state one;
    otherwise ``strategies`` plus ``payload_kwargs`` go through
    :func:`make_crawl_payload`, whose ``**extra`` lands at the top level, which
    is why a policy sub-mapping is not one of them.
    """
    payload = (
        {"url": url, "policy": policy}
        if policy is not None
        else make_crawl_payload(url, strategies=strategies, **payload_kwargs)
    )
    kwargs: dict[str, Any] = {"cache": HttpCache() if cache is None else cache}
    if http_fetcher is not None:
        kwargs["http_fetcher"] = http_fetcher
    if browser_worker is not None:
        kwargs["browser_worker"] = browser_worker
    if content_cache is not None:
        kwargs["content_cache"] = content_cache
    return (await crawl_request(payload, **kwargs))["results"][0]


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


#: The origin every chain test crawls. Named because a chain test's URL is
#: almost never what it is about -- the ladder, the classification, the
#: redaction -- and it was spelled at every call site, so the scheduler's
#: per-domain limit was keyed on a domain half the tests spelled differently.
EXAMPLE_URL = "http://example.com/"


async def run_chain(
    url: str = EXAMPLE_URL,
    fetcher: Any = None,
    *,
    cffi_fetcher: Any = None,
    policy: CrawlPolicy | dict[str, Any],
    scheduler: Any = None,
    browser_worker: Any = None,
) -> Any:
    """What the strategy chain decides for one URL, with the real wiring.

    Both fetcher slots, the domain derived from the URL, the unrestricted host
    policy and an admitting scheduler are the arrangement every chain test
    needed, and a copy that dropped a slot would quietly exercise a different
    chain than the test describes. ``scheduler`` is a parameter so a test can
    inspect the cooldown or delay the chain set on it.

    ``cffi_fetcher`` is a parameter for the tests whose point is that a ladder
    step answers differently from the one before it -- the cookie-retry and
    cffi-escalation suites fail or succeed per transport on purpose. Omitting it
    means "the cffi step behaves like the http step", which is what a
    single-fetcher chain test means.
    """
    from app.crawl.strategy_chain import execute_crawl_chain
    from app.scheduler import DomainScheduler

    if scheduler is None:
        scheduler = DomainScheduler(global_limit=1, per_domain_limit=1, delay_seconds=0.0)
    return await execute_crawl_chain(
        original_url=url,
        normalized_url=url,
        policy=read_policy(policy),
        http_fetcher=fetcher,
        cffi_fetcher=fetcher if cffi_fetcher is None else cffi_fetcher,
        browser_worker=browser_worker,
        scheduler=scheduler,
        domain=urlparse(url).netloc,
        host_policy=UNRESTRICTED_HOST_POLICY,
    )


async def run_cached_twice(
    url: str, policy: CrawlPolicy | dict[str, Any], fetcher: Any, cache: Any, content_cache: Any
):
    """The same URL through the HTTP strategy twice, on the same two caches.

    The content cache is a claim about the *second* run, so the tests that make
    it each ran the strategy twice through an arrangement of their own; the
    second result is the interesting one and the arrangement is not.
    """
    results = [
        await run_http_strategy(url, policy, fetcher, cache=cache, content_cache=content_cache)
        for _ in range(2)
    ]
    return results[0], results[1]


def clocked_cache(
    directory: str,
    *,
    default_ttl: float = 1,
    clock_start: float = 1000.0,
    **settings: Any,
) -> tuple[ContentCache, list[float]]:
    """A cache over ``directory`` reading time from a handle the caller advances.

    Expiry is the subject of most of these tests, so the clock is the thing a
    test has to move, and a handle is what makes moving it possible without
    re-deciding how the cache reads time. Returning it beside the cache is why
    no site has to declare a one-element list and a lambda over it.
    """
    clock = [clock_start]
    return ContentCache(directory, default_ttl=default_ttl, clock=lambda: clock[0], **settings), clock


def make_seeded_cache(directory: str, *keys: str, value: Any = None, **settings: Any):
    """:func:`clocked_cache` holding one entry per key.

    Seeding and the clock are the two halves of "an entry that expires", and a
    test that needs both was writing down the directory, the TTL, the clock list
    and the entry -- four things to keep in agreement.
    """
    cache, clock = clocked_cache(directory, **settings)
    for key in keys:
        cache.set(key, {"data": "1"} if value is None else value)
    return cache, clock


def _spawn_fetch(fn):
    from tests.helpers.server import spawn_server

    server = spawn_server()
    try:
        return fn(server)
    finally:
        server.stop()


def strategy_request(url: str, policy: CrawlPolicy | dict[str, Any], strategy: str) -> StrategyRequest:
    """The page identity these factories hand the real strategy entry points.

    One spelling, so a factory cannot pass one page's URL to a result builder
    that expects another's -- the same mixing hazard the record exists to remove
    in production, and the reason the factories are not free to improvise the
    five fields. The tests drive the real entry points, so the wiring they set up
    is the wiring production uses.
    """
    return StrategyRequest(
        original_url=url,
        normalized_url=url,
        strategy=strategy,
        policy=read_policy(policy),
        host_policy=HostPolicy.unrestricted(),
    )


async def run_http_strategy(
    url: str,
    policy: CrawlPolicy | dict[str, Any],
    http_fetcher: Any,
    cache: Any = None,
    content_cache: Any = None,
    **extra: Any,
) -> Any:
    from app.crawl.strategies.http import _http_strategy_result

    kwargs: dict[str, Any] = {}
    if cache is not None:
        kwargs["cache"] = cache
    if content_cache is not None:
        kwargs["content_cache"] = content_cache
    return await _http_strategy_result(
        request=strategy_request(url, policy, HTTP_DECLARED_CRAWLER),
        http_fetcher=http_fetcher,
        **kwargs,
        **extra,
    )


async def invoke_http_strategy(
    monkeypatch: Any,
    *,
    policy: CrawlPolicy | dict[str, Any],
    resp: Any,
    dedupe_patch: bool = False,
    global_concurrency: int | None = None,
    cap_capture: list[int] | None = None,
) -> Any:
    if dedupe_patch:
        import trafilatura.core

        monkeypatch.setattr(trafilatura.core, "duplicate_test", lambda *a, **k: False)

    def answer(url, _sent_headers, *_):
        return resp(url)

    kwargs: dict[str, Any] = {}
    if global_concurrency is not None:
        kwargs["global_concurrency"] = global_concurrency
    return await run_http_strategy(
        url="http://example.com/",
        policy=policy,
        http_fetcher=fetcher_double(answer, record_cap=cap_capture, answer_is_response=True),
        **kwargs,
    )


async def invoke_browser_strategy(
    monkeypatch: Any, *, policy: CrawlPolicy | dict[str, Any], html: str, text: str = "Hello world"
) -> Any:
    from app.crawl.strategies.browser_result import browser_strategy_result

    return await browser_strategy_result(
        request=strategy_request("http://example.com/", policy, "BROWSER_HEADLESS"),
        browser_worker=_fake_browser(html=html, text=text),
    )


def make_html_fetcher(calls: list[str], body: bytes = b"<html><body>page content</body></html>"):
    """A fetcher serving one HTML body for any URL, recording what it was asked for."""
    return fetcher_double(lambda url, *_: {"status": 200, "ctype": "text/html", "body": body}, calls)


async def run_default_http_fetcher(url: str, headers=None):
    from app.crawl.strategies.fetchers import _default_http_fetcher
    from app.validation import HostPolicy

    return await _default_http_fetcher(
        url, {} if headers is None else headers, 30, policy=HostPolicy.unrestricted()
    )


def make_expiring_cache(tmpdir, *, max_total_bytes: int = 10_000):
    """Two entries that expire at the same instant, and the clock to expire them.

    A test about TTL and byte accounting needs a budget it can overrun and two
    entries it can age out, which is the pair of values the loop below seeds.
    """
    return make_seeded_cache(
        tmpdir,
        "a",
        "b",
        value="x" * 100,
        default_ttl=10,
        max_entries=100,
        max_total_bytes=max_total_bytes,
    )


__all__ = [
    "LLM_FORMAT_TEST_HTML",
    "SCOPE_KEEP_DROP_PAGES",
    "_spawn_fetch",
    "build_payload",
    "cache_with_etag",
    "crawl_homepage_async",
    "crawled_result",
    "clocked_cache",
    "fetcher_double",
    "invoke_browser_strategy",
    "invoke_http_strategy",
    "make_crawl_payload",
    "make_expiring_cache",
    "make_fetch",
    "make_html_fetcher",
    "make_seeded_cache",
    "failing_fetcher",
    "concurrent_fetcher",
    "hanging_fetcher",
    "ok_fetcher",
    "raise_fetcher",
    "site_linking_to",
    "Route",
    "request_fetcher",
    "routed_fetcher",
    "run_cached_twice",
    "run_chain",
    "scripted_fetcher",
    "sequenced_fetcher",
    "tap_fetcher",
    "run_crawl_async",
    "run_default_http_fetcher",
    "run_http_strategy",
    "run_scoped_crawl",
    "site_fetcher",
]
