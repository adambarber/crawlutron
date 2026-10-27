"""Crawl runners: payloads, pages, chains and strategy entry."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, TypedDict, Unpack
from urllib.parse import urlparse

from app.admission.policy import read_policy
from app.models.host_policy import HostPolicy
from app.models.http_fetch_result import HttpFetchResult
from app.orchestration.engine import crawl_request
from app.transport.cache import HttpCache
from app.transport.content_cache import CONTENT_CACHE_MAX_TOTAL_BYTES, ContentCache
from app.transport.fetchers import default_http_fetcher
from app.transport.strategy_request import StrategyRequest
from app.vocabulary.strategies import HTTP_DECLARED_CRAWLER
from tests._fetch_doubles import PageData, PageMap, site_fetcher
from tests._fetch_scripted import tap_fetcher
from tests._policy_factories import UNRESTRICTED_HOST_POLICY, canonical

if TYPE_CHECKING:
    from app.admission.policy import CrawlPolicy
    from app.models.crawl_result import EngineCrawlResult
    from app.models.crawl_worker import CrawlWorker
    from app.models.http_fetcher import HttpFetcher
    from app.models.result_record import ProducerEnvelope
    from app.models.taxonomy import CrawlStrategyName
    from app.runtime.pressure_gates import ResourceGate
    from app.transport.scheduler import DomainScheduler

#: The policy limits a scripted crawl is run under, in wire shape.
Limits = dict[str, object]


class _CrawlPayloadKwargs(TypedDict, total=False):
    """The limit knobs :func:`crawled_result` forwards to ``make_crawl_payload``;
    mypy checks the keys against that function's own signature, so a rename
    fails here and not as a silent default at one caller."""

    strategies: Sequence[str]
    timeout_seconds: int
    max_retries: int
    max_discovered_links: int


class _CrawledRunKwargs(TypedDict, total=False):
    """The run keywords :func:`crawled_urls` forwards: the two knobs a recursive
    test varies. ``url`` is an explicit parameter, not a bag key -- a url/limits/
    policy record would restate the request models' field set (#412)."""

    limits: Limits
    policy: dict[str, object]


def make_crawl_payload(
    url: str,
    *,
    strategies: Sequence[str] | None = None,
    timeout_seconds: int = 10,
    max_retries: int = 0,
    max_discovered_links: int = 50,
    **extra: object,
) -> dict[str, object]:
    payload: dict[str, object] = {
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
    urls: Sequence[str] | None = None,
    strategies: Sequence[str] | None = None,
    limits: Limits | None = None,
    **extra: object,
) -> dict[str, object]:
    payload: dict[str, object] = {}
    if url:
        payload["url"] = url
    if urls:
        payload["urls"] = urls
    policy_block: dict[str, object] = {}
    if strategies is not None:
        policy_block["strategies"] = strategies
    if limits is not None:
        policy_block["limits"] = limits
    if policy_block:
        payload["policy"] = policy_block
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


def xml_page(body: str) -> PageData:
    """One ``application/xml`` page, in the shape a page map wants.

    The content type is what makes a served document a sitemap; a fixture that
    leaves it out exercises the link fallback and passes while sitemaps break."""
    return (200, body, "application/xml")


# Deep enough to follow the seed's links, wide enough that the page cap is never what a test is measuring.
LINKED_PAIR_LIMITS = {"max_depth": 1, "max_pages": 10}


async def crawled_results(
    pages: PageMap,
    *,
    url: str = "http://example.com/",
    limits: Limits | None = None,
    policy: dict[str, object] | None = None,
    spill_path: str | None = None,
    content_cache: ContentCache | None = None,
    calls: list[str] | None = None,
    resource_gate: ResourceGate | None = None,
) -> ProducerEnvelope:
    """Run a recursive crawl over scripted ``pages`` and return the API response.

    Payload, fetcher and cache wiring are stated once so a hand-rolled call
    cannot differ from a routed one; ``limits`` is the limits sub-mapping."""
    body: dict[str, object] = {"limits": dict(limits or {}), **(policy or {})}
    fetcher = tap_fetcher(site_fetcher(pages), calls) if calls is not None else site_fetcher(pages)
    return await crawl_request(
        {"url": url, "policy": body},
        http_fetcher=fetcher,
        # The cffi impersonation slot binds the same scripted fetcher: a leaf with an
        # empty body would otherwise ride the REAL curl_cffi transport (not hermetic).
        cffi_fetcher=fetcher,
        cache=HttpCache(),
        spill_path=spill_path,
        content_cache=content_cache,
        resource_gate=resource_gate,
    )


async def crawled_urls(
    pages: PageMap, *, url: str = "http://example.com/", **kw: Unpack[_CrawledRunKwargs]
) -> list[str]:
    """:func:`crawled_results` projected to the crawled URLs, in crawl order.

    Order is crawl order (BFS), which a caller may depend on; a caller that
    only asserts membership wraps this in ``set()``.
    """
    out = await crawled_results(pages, url=url, **kw)
    # Projecting it as `str | None` would push that possibility onto every caller, and a caller comparing a
    # `str | None` to a canonical URL is the comparison that quietly stops meaning anything.
    return [r.normalized_url.raw for r in out["results"] if r.normalized_url is not None]


def site_linking_to(*paths: str, bodies: dict[str, str] | None = None) -> PageMap:
    """A fake site: an index linking to each of ``paths``, plus a page per path.

    A test names the children it wants; depth, cap and scope stay with the
    caller, and the default child body is its own path, for readable diffs."""
    bodies = bodies or {}
    links = "".join(f'<a href="{path}">{path.strip("/") or path}</a>' for path in paths)
    return {
        "/": (200, f"<html><body>{links}</body></html>", "text/html"),
        **{path: (200, bodies.get(path, f"<html>{path.strip('/')}</html>"), "text/html") for path in paths},
    }


def cache_with_etag(url: str, etag: str = "v1") -> HttpCache:
    """An HTTP cache that already holds a validator for ``url``.

    The 304 path is unreachable until something is stored -- one decision
    keeps the validator, its URL and the cache object together."""
    cache = HttpCache()
    # Value-keyed cache: admit under the same policy the harness admits request URLs with, or the conditional
    # GET silently misses.
    cache.store(canonical(url), {"etag": etag})
    return cache


async def crawled_result(
    url: str,
    *,
    policy: CrawlPolicy | dict[str, object] | None = None,
    http_fetcher: HttpFetcher | None = None,
    browser_worker: CrawlWorker | None = None,
    cache: HttpCache | None = None,
    content_cache: ContentCache | None = None,
    **payload: Unpack[_CrawlPayloadKwargs],
) -> EngineCrawlResult:
    """The one result a real ``crawl_request`` returns for ``url``.

    The single-URL sibling of :func:`crawled_results`: wiring plus the
    ``results[0]`` projection stated once."""
    wire = {"url": url, "policy": policy} if policy is not None else make_crawl_payload(url, **payload)
    return (
        await crawl_request(
            wire,
            cache=HttpCache() if cache is None else cache,
            http_fetcher=http_fetcher,
            browser_worker=browser_worker,
            content_cache=content_cache,
        )
    )["results"][0]


async def run_crawl_async(
    payload: dict[str, object], *, cache: HttpCache | None = None, browser_worker: CrawlWorker | None = None
) -> ProducerEnvelope:
    if cache is None:
        cache = HttpCache()
    return await crawl_request(payload, browser_worker=browser_worker, cache=cache)


async def crawl_homepage_async(
    base_url: str,
    *,
    strategies: Sequence[str] | None = None,
    limits: Limits | None = None,
    cache: HttpCache | None = None,
    browser_worker: CrawlWorker | None = None,
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
    fetcher: HttpFetcher | None = None,
    *,
    cffi_fetcher: HttpFetcher | None = None,
    policy: CrawlPolicy | dict[str, object],
    scheduler: DomainScheduler | None = None,
    browser_worker: CrawlWorker | None = None,
) -> EngineCrawlResult:
    """What the strategy chain decides for one URL, with the real wiring.

    Fetcher slots, host policy and scheduler set once (``scheduler`` stays a
    parameter); omitting ``cffi_fetcher`` means it behaves like the http step."""
    from app.orchestration.strategy_chain import execute_crawl_chain
    from app.transport.scheduler import DomainScheduler

    if scheduler is None:
        scheduler = DomainScheduler(global_limit=1, per_domain_limit=1, delay_seconds=0.0)
    fetcher = fetcher or default_http_fetcher
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
    url: str,
    policy: CrawlPolicy | dict[str, object],
    fetcher: HttpFetcher,
    cache: HttpCache,
    content_cache: ContentCache,
) -> tuple[EngineCrawlResult, EngineCrawlResult]:
    """The same URL through the HTTP strategy twice, on the same two caches.

    The content cache is a claim about the SECOND run, so the arrangement is
    stated once rather than per test."""
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
    max_entries: int = 10000,
    max_total_bytes: int | None = CONTENT_CACHE_MAX_TOTAL_BYTES,
) -> tuple[ContentCache, list[float]]:
    """A cache over ``directory`` reading time from a handle the caller advances.

    Expiry is the subject of most of these tests, so a movable clock handle is
    returned beside the cache -- no site declares a list and a lambda over it."""
    clock = [clock_start]
    return (
        ContentCache(
            directory,
            default_ttl=default_ttl,
            clock=lambda: clock[0],
            max_entries=max_entries,
            max_total_bytes=max_total_bytes,
        ),
        clock,
    )


def make_seeded_cache(
    directory: str,
    *keys: str,
    value: object = None,
    default_ttl: float = 1,
    max_entries: int = 10000,
    max_total_bytes: int | None = CONTENT_CACHE_MAX_TOTAL_BYTES,
) -> tuple[ContentCache, list[float]]:
    """:func:`clocked_cache` holding one entry per key.

    Seeding + a clock are the two halves of "an entry that expires"; a test
    needing both used to write down directory, TTL, clock list and entry."""
    cache, clock = clocked_cache(
        directory, default_ttl=default_ttl, max_entries=max_entries, max_total_bytes=max_total_bytes
    )
    for key in keys:
        cache.set(key, {"data": "1"} if value is None else value)
    return cache, clock


def _spawn_fetch(fn: Callable[[object], HttpFetchResult]) -> HttpFetchResult:
    from tests.helpers.server import spawn_server

    server = spawn_server()
    try:
        return fn(server)
    finally:
        server.stop()


def strategy_request(
    url: str, policy: CrawlPolicy | dict[str, object], strategy: CrawlStrategyName
) -> StrategyRequest:
    """The page identity these factories hand the real strategy entry points.

    One spelling, so a factory cannot pass one page's URL to a builder that
    expects another's -- the same mixing hazard the record removes in production."""
    return StrategyRequest(
        original_url=url,
        normalized_url=canonical(url),
        strategy=strategy,
        policy=read_policy(policy),
        host_policy=HostPolicy.unrestricted(),
    )


class _CrawlHttpKwargs(TypedDict, total=False):
    """The ``http_strategy_result`` keywords a scripted run varies."""

    cache: HttpCache
    content_cache: ContentCache
    caller_validators: dict[str, str] | None
    robots_sitemaps: list[str]
    global_concurrency: int


async def run_http_strategy(
    url: str,
    policy: CrawlPolicy | dict[str, object],
    http_fetcher: HttpFetcher,
    **kwargs: Unpack[_CrawlHttpKwargs],
) -> EngineCrawlResult:
    from app.transport.http import http_strategy_result

    return await http_strategy_result(
        request=strategy_request(url, policy, HTTP_DECLARED_CRAWLER), http_fetcher=http_fetcher, **kwargs
    )
