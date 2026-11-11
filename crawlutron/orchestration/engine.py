"""Crawl execution engine: owns the policy-driven crawl orchestration --
payload/URL/policy parsing, the per-URL strategy chain (HTTP then browser
fallback), worker dispatch, and final result assembly. The single entry point crawlutron.main calls.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable, Mapping
from contextvars import Token
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from crawlutron.admission.admission_links import compile_scope_pattern
from crawlutron.admission.payload_helpers import checked_payload_urls, payload_policy
from crawlutron.admission.policy import PolicyTier
from crawlutron.config.env import get_env_int
from crawlutron.config.settings import CrawlerSettings
from crawlutron.models.crawl_request import CrawlRequest
from crawlutron.models.host_policy import HostPolicy
from crawlutron.models.normalized_url import NormalizedUrl
from crawlutron.models.result_record import ProducerEnvelope
from crawlutron.orchestration.request_pipeline import (
    PipelineDependencies,
    PipelineState,
    run_crawl_request,
)
from crawlutron.runtime import process_state
from crawlutron.runtime.pressure_gates import ResourceGate
from crawlutron.storage.result_store import CrawlSpillStore
from crawlutron.transport import robots as _robots_policy
from crawlutron.transport.cache import HttpCache, shared_http_cache
from crawlutron.transport.content_cache import ContentCache, shared_content_cache
from crawlutron.transport.content_cache_key import build_content_cache_key
from crawlutron.transport.cookie_jar import COOKIE_NAMESPACE
from crawlutron.transport.fetchers import default_http_fetcher, impersonating_fetcher
from crawlutron.transport.request_profile import cookie_namespace_for, headers_for_strategy
from crawlutron.transport.robots import FetchAndDecode

if TYPE_CHECKING:
    from crawlutron.models.crawl_policy import CrawlPolicy
    from crawlutron.models.crawl_worker import CrawlWorker
    from crawlutron.models.http_fetcher import HttpFetcher
    from crawlutron.models.webhook_target import WebhookTarget

logger = logging.getLogger(__name__)
robots_cache = _robots_policy.robots_cache
RobotsMatcher = _robots_policy.RobotsMatcher
load_robots_rules = _robots_policy.load_robots_rules
cached_sitemaps = _robots_policy.cached_sitemaps

# Read the gate via resource_gate(): do not capture the reference before the reset swaps it.
_resource_gate = ResourceGate()


def resource_gate() -> ResourceGate:
    """The process admission gate the engine reserves against."""
    return _resource_gate


def purge_robots_cache(now: float | None = None) -> int:
    return robots_cache.purge_expired(now=now)


def _utc_timestamp(dt: datetime | None) -> float | None:
    """Seconds since the epoch, reading a naive datetime as UTC.
    A sitemap lastmod arrives via RFC-822 or ISO-8601 parsers, both returning a
    naive value for a stamp without an offset, so one place says "UTC is meant". None in, None out.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def parse_lastmod(lastmod: str | None) -> float | None:
    """Parse a sitemap <lastmod> (ISO-8601 or RFC-822) into epoch seconds.
    A stamp beyond CRAWLUTRON_SITEMAP_LASTMOD_FUTURE_SKEW_SECONDS ahead is an
    untrustworthy claim, parsing to None (never "fresher than now"); the seed fails open to re-fetch.
    """
    if not lastmod:
        return None
    s = lastmod.strip()
    ts: float | None = None
    try:
        from email.utils import parsedate_to_datetime

        ts = _utc_timestamp(parsedate_to_datetime(s))
    except (TypeError, ValueError):
        pass
    if ts is None:
        iso = s.replace("Z", "+00:00")
        try:
            ts = _utc_timestamp(datetime.fromisoformat(iso))
        except ValueError:
            return None
    if ts is None:
        return None
    skew = get_env_int("CRAWLUTRON_SITEMAP_LASTMOD_FUTURE_SKEW_SECONDS", 86400, lo=0)
    # wall: lastmod is the origin's published instant, freshness vs its TTL.
    if ts > time.time() + skew:
        return None
    return ts


class SeedCache(Protocol):
    """What the seed-freshness check reads off a cache; ContentCache is the
    reference implementation, and test doubles satisfy it structurally."""

    def cached_at_of(self, key: str) -> float | None: ...


def seed_is_fresh(
    url: NormalizedUrl,
    lastmod: str | None,
    content_cache: SeedCache | None,
    policy: CrawlPolicy,
) -> bool:
    """True if ``url`` is cached and the sitemap ``lastmod`` is not newer than
    our cache timestamp, i.e. our cached copy is fresh and the crawl can skip
    re-fetching it.
    """
    if content_cache is None or not lastmod:
        return False
    lastmod_ts = parse_lastmod(lastmod)
    if lastmod_ts is None:
        return False
    extraction = policy.extraction
    css = policy.css_selector
    xp = policy.xpath
    # The engine's cache keys use the real strategy chain: the old placeholder's keys
    # never matched stored entries, so freshness was never detected through this path.
    for strategy in policy.strategies:
        key = build_content_cache_key(
            url,
            strategy,
            headers_for_strategy(strategy, policy, url),
            extraction,
            css,
            xp,
            cookie_namespace=cookie_namespace_for(policy.request_profile),
        )
        cached_at = content_cache.cached_at_of(key)
        if cached_at is not None and lastmod_ts <= cached_at:
            return True
    return False


def stale_seeds(
    candidates: Iterable[tuple[NormalizedUrl, str | None]],
    content_cache: SeedCache | None,
    policy: CrawlPolicy,
) -> set[NormalizedUrl]:
    """Batch the sitemap freshness filter (call from a worker thread): each verdict
    reads the candidate's envelope from disk, so a big sitemap must not pay that
    cost on the event loop. ``seed_is_fresh`` stays fail-open to re-crawl.
    """
    return {url for url, lastmod in candidates if seed_is_fresh(url, lastmod, content_cache, policy)}


def _set_cookie_namespace(policy: CrawlPolicy):
    return COOKIE_NAMESPACE.set(cookie_namespace_for(policy.request_profile))


def _reset_cookie_namespace(token: Token[str]) -> None:
    COOKIE_NAMESPACE.reset(token)


def _bind_spill(spill_store: CrawlSpillStore | None) -> tuple[CrawlSpillStore, bool]:
    """The crawl's spill store: a caller-supplied shared one, or this crawl's
    private in-memory store (the second element says the caller owns none)."""
    if spill_store is not None:
        return spill_store, False
    return CrawlSpillStore(":memory:"), True


def _admission_preamble(
    payload: CrawlRequest | Mapping[str, object],
    settings: CrawlerSettings | None,
    url_pairs: list[tuple[str, NormalizedUrl]] | None,
) -> tuple[CrawlRequest, CrawlerSettings, HostPolicy, list[tuple[str, NormalizedUrl]], CrawlPolicy]:
    """The one admission: wire coercion, batch cap, policy resolution -- the same
    gate the async endpoint uses before accepting, so the doors cannot drift."""
    if not isinstance(payload, CrawlRequest):
        payload = CrawlRequest.model_validate(dict(payload))
    active_settings = settings or CrawlerSettings.from_mapping()
    host_policy = HostPolicy.from_settings(active_settings)
    pairs = checked_payload_urls(
        payload, active_settings.max_batch_size, url_pairs=url_pairs, host_policy=host_policy
    )
    return (
        payload,
        active_settings,
        host_policy,
        pairs,
        payload_policy(payload, PolicyTier.from_settings(active_settings)),
    )


async def crawl_request(
    payload: CrawlRequest | Mapping[str, object],
    *,
    settings: CrawlerSettings | None = None,
    url_pairs: list[tuple[str, NormalizedUrl]] | None = None,
    browser_worker: CrawlWorker | None = None,
    http_fetcher: HttpFetcher | None = None,
    cffi_fetcher: HttpFetcher | None = None,
    cache: HttpCache | None = None,
    content_cache: ContentCache | None = None,
    webhook_url: WebhookTarget | None = None,
    shutdown_event: asyncio.Event | None = None,
    spill_store: CrawlSpillStore | None = None,
    fetch_and_decode: FetchAndDecode | None = None,
    resource_gate: ResourceGate | None = None,
) -> ProducerEnvelope:
    """One typed crawl through every entry point. ``payload`` is a model; a wire
    mapping is validated once to it here (the seam's single coercion), so every
    downstream read is typed -- ``payload.policy``, ``payload.url``, never a dict.
    """
    payload, active_settings, host_policy, url_pairs, policy = _admission_preamble(
        payload, settings, url_pairs
    )
    fetcher = http_fetcher or default_http_fetcher
    # A binding of the one fetcher, not a second entry point: the impersonating
    # strategy takes the same callable with the cffi transport preferred.
    cffi = cffi_fetcher or impersonating_fetcher()
    shutdown_event = shutdown_event or asyncio.Event()
    # Bind the cookie namespace for this crawl's lifetime so concurrent tenants
    # cannot leak through the shared jar; "" keeps single-tenant callers unchanged.
    _cookie_token = _set_cookie_namespace(policy)
    spill, private_spill = _bind_spill(spill_store)
    state: PipelineState | None = None
    try:
        state = PipelineState(
            payload=payload,
            settings=active_settings,
            url_pairs=url_pairs,
            policy=policy,
            fetcher=fetcher,
            cffi=cffi,
            host_policy=host_policy,
            shutdown_event=shutdown_event,
            browser_worker=browser_worker,
            cache=cache,
            content_cache=content_cache,
            webhook_url=webhook_url,
            spill_store=spill,
            dependencies=_pipeline_dependencies(fetch_and_decode, resource_gate),
        )
        return await run_crawl_request(state)
    finally:
        _reset_cookie_namespace(_cookie_token)
        _teardown_spill(spill, state, private_spill)


def _teardown_spill(spill: CrawlSpillStore, state: PipelineState | None, private_spill: bool) -> None:
    """The crawl's spill teardown: the writer's own release -- once the crawl has
    ended its rows are served or dead and the store may age-reclaim them -- then
    the private store's close, never the shared one's."""
    minted = state.request_id if state is not None else None
    if minted is not None:
        spill.release_spill(minted)
    if private_spill:
        spill.close()


def _pipeline_dependencies(
    fetch_and_decode: FetchAndDecode | None,
    resource_gate: ResourceGate | None,
) -> PipelineDependencies:
    """The process-wide dependencies a crawl binds: shared caches and gate."""
    return PipelineDependencies(
        get_content_cache=shared_content_cache,
        compile_scope_regex_safe=compile_scope_pattern,
        stale_seeds=stale_seeds,
        fetch_and_decode=fetch_and_decode,
        shared_http_cache=shared_http_cache(),
        resource_gate=resource_gate if resource_gate is not None else _resource_gate,
    )


def _reset() -> None:
    """Rebuild every process-wide resource the engine shares across crawls."""
    robots_cache.clear()
    global _resource_gate
    # The shared admission gate, reset with the caches; see :func:`resource_gate`.
    _resource_gate = ResourceGate()


def _has() -> bool:
    """Whether the engine's own shared resource still holds state."""
    return bool(robots_cache)


process_state.register(__name__, _reset, _has)
