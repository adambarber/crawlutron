"""Crawl runners: payloads, pages, chains and strategy entry."""

from typing import Any
from urllib.parse import urlparse

from app.admission.policy import CrawlPolicy, read_policy
from app.models.crawl_result import EngineCrawlResult
from app.models.host_policy import HostPolicy
from app.models.result_record import ProducerEnvelope
from app.models.taxonomy import CrawlStrategyName
from app.orchestration.engine import crawl_request
from app.transport.cache import HttpCache
from app.transport.content_cache import ContentCache
from app.transport.strategy_request import StrategyRequest
from app.vocabulary.strategies import HTTP_DECLARED_CRAWLER
from tests._fetch_doubles import site_fetcher
from tests._fetch_scripted import tap_fetcher
from tests._policy_factories import UNRESTRICTED_HOST_POLICY, canonical


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
    limits: dict[str, Any] | None = None,
    **extra,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
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


# Deep enough to follow the seed's links, wide enough that the page cap is never what a test is measuring.
LINKED_PAIR_LIMITS = {"max_depth": 1, "max_pages": 10}


async def crawled_results(
    pages: dict[str, Any],
    *,
    url: str = "http://example.com/",
    limits: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
    spill_path: str | None = None,
    content_cache: Any = None,
    calls: list[str] | None = None,
    resource_gate: Any = None,
) -> ProducerEnvelope:
    """Run a recursive crawl over scripted ``pages`` and return the API response.

    The one way a test should invoke ``crawl_request`` against a scripted site.
    The payload shape, the fetcher wiring and the cache wiring are stated once
    here, so a policy key or a signature change lands here rather than in each
    recursive test -- and a hand-rolled call cannot quietly differ from a
    routed one, which is the drift this chokepoint exists to prevent.

    ``limits`` is the limits sub-mapping; ``policy`` carries the rest of the
    policy (``discovery``, ``scope_regex``, ``browser_profile``) as a mapping,
    which is the shape the body below unpacks it as -- a ``CrawlPolicy`` is a
    model, and ``**model`` is not a thing. ``calls``, if given, records the URLs
    that actually reached the network, so a test about caching does not have to
    build its own tap.
    """
    body: dict[str, Any] = {"limits": dict(limits or {}), **(policy or {})}
    fetcher = tap_fetcher(site_fetcher(pages), calls) if calls is not None else site_fetcher(pages)
    return await crawl_request(
        {"url": url, "policy": body},
        http_fetcher=fetcher,
        cache=HttpCache(),
        spill_path=spill_path,
        content_cache=content_cache,
        resource_gate=resource_gate,
    )


async def crawled_urls(pages: dict[str, Any], **kwargs: Any) -> list[str]:
    """:func:`crawled_results` projected to the crawled URLs, in crawl order.

    Order is crawl order (BFS), which a caller may depend on; a caller that
    only asserts membership wraps this in ``set()``.
    """
    out = await crawled_results(pages, **kwargs)
    # Projecting it as `str | None` would push that possibility onto every caller, and a caller comparing a
    # `str | None` to a canonical URL is the comparison that quietly stops meaning anything.
    return [r.normalized_url.raw for r in out["results"] if r.normalized_url is not None]


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
    # Value-keyed cache: admit under the same policy the harness admits request URLs with, or the conditional
    # GET silently misses.
    cache.store(canonical(url), {"etag": etag})
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
) -> EngineCrawlResult:
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


async def run_crawl_async(payload: dict[str, Any], *, cache=None, browser_worker=None) -> ProducerEnvelope:
    if cache is None:
        cache = HttpCache()
    return await crawl_request(payload, browser_worker=browser_worker, cache=cache)


async def crawl_homepage_async(
    base_url: str,
    *,
    strategies: list[str] | None = None,
    limits: dict[str, Any] | None = None,
    cache=None,
    browser_worker=None,
) -> ProducerEnvelope:
    payload = build_payload(
        url=f"{base_url}/",
        strategies=strategies if strategies is not None else [HTTP_DECLARED_CRAWLER],
        limits=limits
        if limits is not None
        else {"timeout_seconds": 10, "max_retries": 0, "max_discovered_links": 50},
    )
    return await run_crawl_async(payload, cache=cache, browser_worker=browser_worker)


#: The origin every chain test crawls.
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
    from app.orchestration.scheduler import DomainScheduler
    from app.orchestration.strategy_chain import execute_crawl_chain

    if scheduler is None:
        scheduler = DomainScheduler(global_limit=1, per_domain_limit=1, delay_seconds=0.0)
    return await execute_crawl_chain(
        original_url=url,
        normalized_url=canonical(url),
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


def strategy_request(
    url: str, policy: CrawlPolicy | dict[str, Any], strategy: CrawlStrategyName
) -> StrategyRequest:
    """The page identity these factories hand the real strategy entry points.

    One spelling, so a factory cannot pass one page's URL to a result builder
    that expects another's -- the same mixing hazard the record exists to remove
    in production, and the reason the factories are not free to improvise the
    five fields. The tests drive the real entry points, so the wiring they set up
    is the wiring production uses.
    """
    return StrategyRequest(
        original_url=url,
        normalized_url=canonical(url),
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
    from app.transport.http import http_strategy_result

    kwargs: dict[str, Any] = {}
    if cache is not None:
        kwargs["cache"] = cache
    if content_cache is not None:
        kwargs["content_cache"] = content_cache
    return await http_strategy_result(
        request=strategy_request(url, policy, HTTP_DECLARED_CRAWLER),
        http_fetcher=http_fetcher,
        **kwargs,
        **extra,
    )
