"""Crawl execution engine: owns the policy-driven crawl orchestration --
payload/URL/policy parsing, the per-URL strategy chain (HTTP then browser
fallback), worker dispatch, and final result assembly. The single entry point crawlutron_rest.main calls.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable, Mapping
from contextvars import Token
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from crawlutron._time import utc_timestamp
from crawlutron.admission.admission_links import compile_scope_pattern
from crawlutron.admission.payload_helpers import checked_payload_urls, payload_policy
from crawlutron.admission.policy import PolicyTier
from crawlutron.config.env import get_env_int
from crawlutron.config.settings import CrawlerSettings
from crawlutron.config.settings_cache import get_cached_settings
from crawlutron.models.crawl_request import CrawlRequest
from crawlutron.models.result_record import ProducerEnvelope
from crawlutron.orchestration.request_pipeline import (
    PipelineDependencies,
    PipelineState,
    run_crawl_request,
)
from crawlutron.orchestration.resource_governor import (
    build_resource_governor,
    install_resource_governor,
    resource_governor,
)
from crawlutron.runtime import process_state
from crawlutron.storage.result_store import CrawlSpillStore
from crawlutron.transport import robots as _robots_policy
from crawlutron.transport.cache import HttpCache, shared_http_cache
from crawlutron.transport.content_cache import ContentCache, shared_content_cache
from crawlutron.transport.content_cache_key import build_content_cache_key
from crawlutron.transport.cookie_jar import COOKIE_NAMESPACE
from crawlutron.transport.fetchers import default_http_fetcher, impersonating_fetcher
from crawlutron.transport.request_profile import cookie_namespace_for, headers_for_strategy
from crawlutron.transport.robots import FetchAndDecode
from crawlutron_admit.sensing.pressure_gates import ResourceGate
from safe_url.host_policy import HostPolicy
from safe_url.normalized_url import NormalizedUrl

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

        ts = utc_timestamp(parsedate_to_datetime(s))
    except (TypeError, ValueError):
        pass
    if ts is None:
        iso = s.replace("Z", "+00:00")
        try:
            ts = utc_timestamp(datetime.fromisoformat(iso))
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


def fresh_seeds(
    candidates: Iterable[tuple[NormalizedUrl, str | None]],
    content_cache: SeedCache | None,
    policy: CrawlPolicy,
) -> set[NormalizedUrl]:
    """Batch the sitemap freshness filter (call from a worker thread), returning
    the cached-fresh seeds the caller excludes; ``seed_is_fresh`` stays fail-open
    to re-crawl.
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
    active_settings = settings or get_cached_settings()
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
        await _teardown_spill(spill, state, private_spill)


async def _teardown_spill(spill: CrawlSpillStore, state: PipelineState | None, private_spill: bool) -> None:
    """The crawl's spill teardown: the writer's release (off-loop via the async
    twin -- a sync write on the loop is refused), then the private store's close."""
    minted = state.request_id if state is not None else None
    if minted is not None:
        await spill.arelease_spill(minted)
    if private_spill:
        spill.close()


def _pipeline_dependencies(
    fetch_and_decode: FetchAndDecode | None,
    resource_gate: ResourceGate | None,
) -> PipelineDependencies:
    """The process-wide dependencies a crawl binds: shared caches and gate."""
    if resource_gate is not None:
        # An explicitly injected gauge is a test seam: size the one governor from it.
        install_resource_governor(build_resource_governor(resource_gate))
    return PipelineDependencies(
        get_content_cache=shared_content_cache,
        compile_scope_regex_safe=compile_scope_pattern,
        fresh_seeds=fresh_seeds,
        fetch_and_decode=fetch_and_decode,
        shared_http_cache=shared_http_cache(),
        resource_gate=resource_gate if resource_gate is not None else _resource_gate,
        resource_governor=resource_governor(),
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
