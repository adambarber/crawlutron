"""Crawl execution engine.

Owns the policy-driven crawl orchestration: payload/URL/policy parsing,
the per-URL strategy chain (HTTP -> browser fallback), worker dispatch, and
the final result assembly. This module is the single entry point that the
FastAPI layer (``app.main``) calls into.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import threading
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from functools import partial
from typing import Any

from app.constants import (
    DEFAULT_EXTRACTION,
    DEFAULT_STRATEGIES,
)
from app.crawl import robots as _robots_policy
from app.resource_pressure import ResourceGate

logger = logging.getLogger(__name__)

ROBOTS_CACHE_MAX_ENTRIES = _robots_policy.ROBOTS_CACHE_MAX_ENTRIES
ROBOTS_CACHE_TTL_SECONDS = _robots_policy.ROBOTS_CACHE_TTL_SECONDS
ROBOTS_FAILURE_TTL_SECONDS = _robots_policy.ROBOTS_FAILURE_TTL_SECONDS
_ROBOTS_PATTERN_CACHE_PER_HOST = _robots_policy.ROBOTS_PATTERN_CACHE_PER_HOST
_ROBOTS_PATTERN_CACHE_MAX_HOSTS = _robots_policy.ROBOTS_PATTERN_CACHE_MAX_HOSTS
robots_cache = _robots_policy.robots_cache
_RobotsPathMatcher = _robots_policy._RobotsPathMatcher
_MatchAll = _robots_policy._MatchAll
_PrefixMatcher = _robots_policy._PrefixMatcher
_RobotsPatternCache = _robots_policy._RobotsPatternCache
RobotsMatcher = _robots_policy.RobotsMatcher
_compile_robots_pattern = _robots_policy._compile_robots_pattern
_robots_pattern_to_regex = _robots_policy._robots_pattern_to_regex
_robots_allows = _robots_policy.robots_allows
load_robots_rules = _robots_policy.load_robots_rules
cached_sitemaps = _robots_policy.cached_sitemaps

_resource_gate = ResourceGate()
from app.crawl.admission import compile_scope_pattern
from app.crawl.cache import HttpCache
from app.crawl.content_cache import ContentCache
from app.crawl.fetch_helpers import _fetch_and_decode
from app.crawl.payload_helpers import _payload_policy, checked_payload_urls
from app.crawl.request_pipeline import _PipelineDependencies, _PipelineState, run_crawl_request
from app.crawl.strategies.fetchers import _default_cffi_fetcher, _default_http_fetcher
from app.models import (
    CrawlWorker,
    EngineCrawlResult,
    HttpFetcher,
    to_dict,
)
from app.settings import CrawlerSettings
from app.validation import (
    HostPolicy,
    get_env_int,
)

_DEFAULT_FETCH_AND_DECODE = _fetch_and_decode

# Shared conditional cache so repeated crawls can leverage 304 responses.
_SHARED_HTTP_CACHE = HttpCache()

# Lazy, disk-backed content cache (#5). Created on first use so importing
# this module has no filesystem side effects (tests can pass their own).
_SHARED_CONTENT_CACHE: ContentCache | None = None


def _get_shared_content_cache() -> ContentCache:
    global _SHARED_CONTENT_CACHE
    if _SHARED_CONTENT_CACHE is None:
        settings = CrawlerSettings.from_mapping()
        _SHARED_CONTENT_CACHE = ContentCache(
            cache_dir=settings.cache_dir,
            default_ttl=settings.cache_ttl_seconds,
        )
    return _SHARED_CONTENT_CACHE








def _purge_robots_cache(now: float | None = None) -> int:
    return robots_cache.purge_expired(now=now)


def _parse_lastmod(lastmod: str | None) -> float | None:
    """Parse a sitemap <lastmod> (ISO-8601 or RFC-822) into epoch seconds.

    A stamp further in the future than ``BROWSER_CRAWLER_SITEMAP_LASTMOD_
    FUTURE_SKEW_SECONDS`` (default 1 day; ``lo=0`` so a negative config can
    never reject the recent past) is an untrustworthy claim, not freshness
    information -- broken generators stamp the whole sitemap "now-plus" on
    every regeneration. It parses to ``None``, the same verdict as an absent
    stamp: the seed fails open to re-fetch, and the freshness check declines
    before composing cache keys, so the garbage costs nothing. The
    degradation direction is deliberate (H7): never "fresher than now".
    """
    if not lastmod:
        return None
    s = lastmod.strip()
    ts: float | None = None
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(s)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            ts = dt.timestamp()
    except (TypeError, ValueError):
        pass
    if ts is None:
        iso = s.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            ts = dt.timestamp()
        except ValueError:
            return None
    if ts is None:
        return None
    skew = get_env_int("BROWSER_CRAWLER_SITEMAP_LASTMOD_FUTURE_SKEW_SECONDS", 86400, lo=0)
    if ts > time.time() + skew:
        return None
    return ts


def _seed_is_fresh(url: str, lastmod: str | None, content_cache: Any, policy: dict[str, Any]) -> bool:
    """True if ``url`` is cached and the sitemap ``lastmod`` is not newer than
    our cache timestamp, i.e. our cached copy is fresh and the crawl can skip
    re-fetching it (#3).
    """
    if content_cache is None or not lastmod:
        return False
    lastmod_ts = _parse_lastmod(lastmod)
    if lastmod_ts is None:
        return False
    extraction = policy.get("extraction") or DEFAULT_EXTRACTION
    css = policy.get("css_selector")
    xp = policy.get("xpath")
    # Lazy import avoids a module cycle (engine <-> http).
    from app.crawl.strategies.content_cache_key import _content_cache_key
    from app.crawl.strategies.request_profile import _headers_for_strategy

    # Default to the real strategy chain (L5): the old ["HTTP"] placeholder
    # is not a strategy, so its cache keys could never match stored entries
    # and freshness was never detected through this path.
    for strategy in policy.get("strategies", DEFAULT_STRATEGIES):
        key = _content_cache_key(
            url,
            strategy,
            _headers_for_strategy(strategy, policy, url),
            extraction,
            css,
            xp,
        )
        cached_at = content_cache.cached_at_of(key)
        if cached_at is not None and lastmod_ts <= cached_at:
            return True
    return False


def _stale_seeds(
    candidates: Iterable[tuple[str, str | None]],
    content_cache: Any,
    policy: dict[str, Any],
) -> set[str]:
    """Batch the sitemap freshness filter (call from a worker thread).

    Each verdict reads and parses the candidate's envelope from disk, so a big
    sitemap must not pay that cost on the event loop: :func:`_seed_is_fresh`
    stays fail-open (unreadable/expired/missing entries are not fresh, so the
    URL is still crawled) and the key composition is untouched.
    """
    return {url for url, lastmod in candidates if _seed_is_fresh(url, lastmod, content_cache, policy)}


_SPILL_PREFIX = "bc-spill-"

# Spill paths a live _ResultSpill still owns, and the single answer the
# stale-spill sweep consults before unlinking. Age is not ownership: a spill
# outlives its own creation by as long as its crawl runs, and the sweep runs
# process-wide, so any spill past gc_age_seconds is reachable by a GC pass while
# its writer is still appending. The lock guards set operations only -- nothing
# is awaited while it is held.
_LIVE_SPILL_PATHS: set[str] = set()
_LIVE_SPILL_LOCK = threading.Lock()

# Cadence gate for _ResultSpill's stale-spill sweep (perf tier-2 T5): at most
# one temp-dir scan per interval per process; None means "never scanned",
# forcing the first construction to sweep immediately.
_SPILL_GC_MIN_INTERVAL_SECONDS = 300.0
_LAST_SPILL_GC_MONOTONIC: float | None = None


class _ResultSpill:
    """Append-only NDJSON spill of page results (#2-stream).

    Each completed page is serialized to disk the moment its batch finishes, so
    peak RSS scales with batch size instead of total pages. RAM keeps only the
    running count; the response is reassembled by reading the file back in
    index order. Auto-created files are deleted on close; stale spills from
    killed runs are garbage-collected when the next crawl starts.
    """

    def __init__(
        self,
        path: str | None = None,
        *,
        gc_age_seconds: float = 3600,
    ) -> None:
        self._own = path is None
        # ``resolved`` is the concrete path on both branches (mkstemp fills in
        # the temp path when the caller gave none), so it is the only value
        # stored on ``self``: ``self.path`` is always a str a reader can open.
        resolved: str
        if self._own:
            fd, resolved = tempfile.mkstemp(prefix=_SPILL_PREFIX, suffix=".ndjson")
            # An exception between mkstemp and fdopen would leak the raw fd
            # AND the on-disk file. Close the fd if fdopen fails (adversarial
            # review F-result-spill-fd-leak).
            try:
                self._fh = os.fdopen(fd, "w", encoding="utf-8")
            except Exception:
                try:
                    os.close(fd)
                except OSError:
                    pass
                try:
                    os.unlink(resolved)
                except OSError:
                    pass
                raise
        else:
            assert path is not None
            resolved = path
            self._fh = open(resolved, "w", encoding="utf-8")
        self.path = resolved
        self.count = 0
        with _LIVE_SPILL_LOCK:
            _LIVE_SPILL_PATHS.add(resolved)
        # Serialize writes (the file handle is shared) while the (potentially
        # large) json.dumps is offloaded off the event loop (adversarial #1.3).
        self._write_lock = asyncio.Lock()
        global _LAST_SPILL_GC_MONOTONIC
        now_mono = time.monotonic()
        if (
            _LAST_SPILL_GC_MONOTONIC is None
            or now_mono - _LAST_SPILL_GC_MONOTONIC >= _SPILL_GC_MIN_INTERVAL_SECONDS
        ):
            # Cadence gate (perf tier-2 T5): the temp-dir sweep is O(dir) and
            # ran per crawl request. The None-initial stamp guarantees the
            # first construction in a process always scans, so spill files
            # orphaned by killed runs are still reclaimed; the age predicate
            # below still protects active spills from early unlink.
            _LAST_SPILL_GC_MONOTONIC = now_mono
            try:
                self._gc_stale_spills(gc_age_seconds)
            except Exception:
                # GC failure must not abort the spill (adversarial #result-spill
                # review). The next interval boundary will retry.
                pass

    def _gc_stale_spills(self, max_age: float) -> None:
        """Delete spill files older than ``max_age`` that no live spill owns.

        Age alone is not permission to unlink. A spill is reassembled by opening
        its path BY NAME in :meth:`load_ordered`, so removing the directory entry
        out from under a running spill does not cost that worker a local
        checkpoint -- it makes the finished crawl's own output unreadable, after
        the crawl has already been paid for. The earlier version of this method
        documented that as an acceptable worst case; it is data loss, and a
        whole-site job living past ``gc_age_seconds`` is ordinary rather than
        exotic, so the sweep consults :data:`_LIVE_SPILL_PATHS` instead of
        reasoning about who might still be reading.

        Unlinking an unowned file is still safe on Unix (a reader holding the fd
        keeps the inode) and raises ``PermissionError`` on Windows, which the
        ``except OSError`` below skips.
        """
        now = time.time()
        try:
            names = os.listdir(tempfile.gettempdir())
        except OSError:
            return
        with _LIVE_SPILL_LOCK:
            in_use = frozenset(_LIVE_SPILL_PATHS)
        for name in names:
            if not name.startswith(_SPILL_PREFIX):
                continue
            candidate = os.path.join(tempfile.gettempdir(), name)
            if candidate in in_use:
                continue
            try:
                if now - os.path.getmtime(candidate) > max_age:
                    os.unlink(candidate)
            except OSError:
                continue

    async def write(self, index: int, result: EngineCrawlResult) -> None:
        # Offload the (potentially large) json.dumps to a worker thread so the
        # event loop is never stalled serializing results (adversarial #1.3).
        line = await asyncio.to_thread(json.dumps, {"i": index, "r": to_dict(result)})
        async with self._write_lock:
            self._fh.write(line + "\n")
            self.count += 1

    def load_ordered(self) -> list[dict[str, Any]]:
        """Reassemble all spilled results in submission order.

        Single pass: each parsed payload is placed directly into its index
        slot — no intermediate tuple list, no O(N log N) sort. The returned
        list still materializes every result: the JSON response contract needs
        the complete ordered set in RAM before the first byte can be written,
        so that materialization is the caller's to pay (``_crawl_request_impl``
        only offloads the file scan and the per-line parse off the event loop).
        Streaming consumers are the documented escape hatch if the full-set
        materialization ever becomes a constraint.
        """
        self._fh.flush()
        slots: dict[int, dict[str, Any]] = {}
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                slots[entry["i"]] = entry["r"]
        return [slots[key] for key in sorted(slots)]

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass
        # Release the in-use claim before the unlink, so the sweep and this
        # cleanup cannot race over the same path.
        with _LIVE_SPILL_LOCK:
            _LIVE_SPILL_PATHS.discard(self.path)
        # Auto-cleanup of owned files: when this spill was created without
        # an explicit path (caller-owned spills retain their file), drop the
        # backing temp file so a happy-path close never orphans it.
        if getattr(self, "_own", False):
            try:
                os.unlink(self.path)
            except OSError:
                pass


def _set_cookie_namespace(policy: dict[str, Any]):
    from app.crawl.strategies.cookie_jar import _COOKIE_NAMESPACE
    from app.crawl.strategies.request_profile import cookie_namespace_for

    return _COOKIE_NAMESPACE.set(cookie_namespace_for(policy.get("request_profile") or {}))


def _reset_cookie_namespace(token: Any) -> None:
    from app.crawl.strategies.cookie_jar import _COOKIE_NAMESPACE

    _COOKIE_NAMESPACE.reset(token)


async def crawl_request(
    payload: dict[str, Any],
    *,
    settings: CrawlerSettings | None = None,
    urls: list[str] | None = None,
    browser_worker: CrawlWorker | None = None,
    http_fetcher: HttpFetcher | None = None,
    cffi_fetcher: HttpFetcher | None = None,
    cache: HttpCache | None = None,
    content_cache: ContentCache | None = None,
    webhook_url: str | None = None,
    shutdown_event: asyncio.Event | None = None,
    spill_path: str | None = None,
) -> dict[str, Any]:
    active_settings = settings or CrawlerSettings.from_mapping()
    host_policy = HostPolicy.from_settings(active_settings)
    # L4: the batch cap is enforced by the same gate the async endpoint uses
    # before accepting, so sync and async cannot drift on oversized batches.
    # A forwarded batch (tier-2 T3) skips re-derivation but not the cap.
    urls = checked_payload_urls(
        payload,
        active_settings.max_batch_size,
        urls=urls,
        host_policy=host_policy,
    )
    policy = _payload_policy(payload, active_settings)
    fetcher = http_fetcher or partial(_default_http_fetcher, policy=host_policy)
    cffi = cffi_fetcher or partial(_default_cffi_fetcher, policy=host_policy)
    shutdown_event = shutdown_event or asyncio.Event()
    # Bind the cookie namespace for the lifetime of this crawl so concurrent
    # multi-tenant calls cannot leak cookies through the shared
    # ``_GLOBAL_COOKIE_JAR``. Defaults to "" (legacy shared behavior) so
    # single-tenant callers and tests see no change (adversarial review
    # F-cookie-cross-contamination).
    _cookie_token = _set_cookie_namespace(policy)
    try:
        state = _PipelineState(
            payload=payload, settings=active_settings, urls=urls, policy=policy,
            fetcher=fetcher, cffi=cffi, host_policy=host_policy, shutdown_event=shutdown_event,
            browser_worker=browser_worker, cache=cache, content_cache=content_cache,
            webhook_url=webhook_url, spill_path=spill_path,
            dependencies=_PipelineDependencies(
                get_content_cache=_get_shared_content_cache, make_spill=_ResultSpill,
                compile_scope_regex_safe=compile_scope_pattern, stale_seeds=_stale_seeds,
                fetch_and_decode=_fetch_and_decode, default_fetch_and_decode=_DEFAULT_FETCH_AND_DECODE,
                shared_http_cache=_SHARED_HTTP_CACHE, resource_gate=_resource_gate,
            ),
        )
        return await run_crawl_request(state)
    finally:
        _reset_cookie_namespace(_cookie_token)
