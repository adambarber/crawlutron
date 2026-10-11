"""Crawl execution engine.

Owns the policy-driven crawl orchestration: payload/URL/policy parsing,
the per-URL strategy chain (HTTP -> browser fallback), worker dispatch, and
the final result assembly. This module is the single entry point that the
FastAPI layer (``app.main``) calls into.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import tempfile
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, cast

from app.admission.admission import compile_scope_pattern
from app.admission.payload_helpers import checked_payload_urls, payload_policy
from app.admission.policy import PolicyTier
from app.config.env import get_env_int
from app.config.settings import CrawlerSettings
from app.models.crawl_policy import CrawlPolicy
from app.models.crawl_worker import CrawlWorker
from app.models.engine_crawl_result import EngineCrawlResult
from app.models.host_policy import HostPolicy
from app.models.http_fetcher import HttpFetcher
from app.models.normalized_url import NormalizedUrl
from app.models.result_record import ProducerEnvelope
from app.models.serialization import from_wire, to_dict
from app.orchestration import robots as _robots_policy
from app.orchestration.request_pipeline import _PipelineDependencies, _PipelineState, run_crawl_request
from app.orchestration.robots import FetchAndDecode
from app.runtime.resource_pressure import ResourceGate
from app.transport.cache import HttpCache
from app.transport.content_cache import ContentCache
from app.transport.fetchers import default_http_fetcher, impersonating_fetcher

logger = logging.getLogger(__name__)

robots_cache = _robots_policy.robots_cache
RobotsMatcher = _robots_policy.RobotsMatcher
load_robots_rules = _robots_policy.load_robots_rules
cached_sitemaps = _robots_policy.cached_sitemaps

_resource_gate = ResourceGate()

# Shared conditional cache so repeated crawls can leverage 304 responses.
_SHARED_HTTP_CACHE = HttpCache()

# Lazy, disk-backed content cache. Created on first use so importing
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


def _utc_timestamp(dt: datetime | None) -> float | None:
    """Seconds since the epoch, reading a naive datetime as UTC.

    Sitemap ``<lastmod>`` arrives in two shapes -- RFC 822 via
    ``parsedate_to_datetime``, ISO 8601 via ``fromisoformat`` -- and each
    parser hands back a *naive* datetime for a stamp that omits an offset,
    which is the common case rather than a malformed one. A naive value has no
    epoch to compare against, so something has to say UTC is meant. The two
    parse branches each said it, which is one assumption living in two places.

    None in, None out: a parser that found nothing is not a timestamp.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def _parse_lastmod(lastmod: str | None) -> float | None:
    """Parse a sitemap <lastmod> (ISO-8601 or RFC-822) into epoch seconds.

    A stamp further in the future than
    ``CRAWLUTRON_SITEMAP_LASTMOD_FUTURE_SKEW_SECONDS`` (default 1 day; ``lo=0`` so a negative config can
    never reject the recent past) is an untrustworthy claim, not freshness
    information -- broken generators stamp the whole sitemap "now-plus" on
    every regeneration. It parses to ``None``, the same verdict as an absent
    stamp: the seed fails open to re-fetch, and the freshness check declines
    before composing cache keys, so the garbage costs nothing. The
    degradation direction is deliberate: never "fresher than now".
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
    if ts > time.time() + skew:
        return None
    return ts


def _seed_is_fresh(url: NormalizedUrl, lastmod: str | None, content_cache: Any, policy: CrawlPolicy) -> bool:
    """True if ``url`` is cached and the sitemap ``lastmod`` is not newer than
    our cache timestamp, i.e. our cached copy is fresh and the crawl can skip
    re-fetching it.
    """
    if content_cache is None or not lastmod:
        return False
    lastmod_ts = _parse_lastmod(lastmod)
    if lastmod_ts is None:
        return False
    extraction = policy.extraction
    css = policy.css_selector
    xp = policy.xpath
    # Lazy import avoids a module cycle (engine <-> http).
    from app.transport.content_cache_key import build_content_cache_key
    from app.transport.request_profile import headers_for_strategy

    # Default to the real strategy chain: the old placeholder's cache keys never
    # matched stored entries, so freshness was never detected through this path.
    for strategy in policy.strategies:
        key = build_content_cache_key(
            url,
            strategy,
            headers_for_strategy(strategy, policy, url),
            extraction,
            css,
            xp,
        )
        cached_at = content_cache.cached_at_of(key)
        if cached_at is not None and lastmod_ts <= cached_at:
            return True
    return False


def _stale_seeds(
    candidates: Iterable[tuple[NormalizedUrl, str | None]],
    content_cache: Any,
    policy: CrawlPolicy,
) -> set[NormalizedUrl]:
    """Batch the sitemap freshness filter (call from a worker thread).

    Each verdict reads and parses the candidate's envelope from disk, so a big
    sitemap must not pay that cost on the event loop: :func:`_seed_is_fresh`
    stays fail-open (unreadable/expired/missing entries are not fresh, so the
    URL is still crawled) and the key composition is untouched.
    """
    return {url for url, lastmod in candidates if _seed_is_fresh(url, lastmod, content_cache, policy)}


_SPILL_PREFIX = "bc-spill-"

# A spill is owned while any process holds an advisory lock on it -- a kernel
# fact, not memory (see :func:`_spill_is_unowned`). Sweep: one scan per interval.
_SPILL_GC_MIN_INTERVAL_SECONDS = 300.0
_LAST_SPILL_GC_MONOTONIC: float | None = None


def _spill_is_unowned(path: str) -> bool:
    """True when no live process holds ``path``'s ownership lock.

    Taking the lock IS the test. The kernel grants it only when no other open
    file description holds it, and drops every holder's lock when its process
    dies -- so a spill abandoned by a killed run is collectable while a spill
    another worker is still appending to is not. That is the distinction age
    alone cannot make, and it holds across processes rather than only within
    this one.

    An unopenable path is reported as owned: the sweep must not delete a file
    whose state it cannot establish.
    """
    try:
        probe = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    else:
        return True
    finally:
        os.close(probe)


class _ResultSpill:
    """Append-only NDJSON spill of page results.

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
        # ``resolved`` is the concrete path on both branches (mkstemp fills a temp
        # path in), so ``self.path`` is always a str a reader can open.
        resolved: str
        if self._own:
            fd, resolved = tempfile.mkstemp(prefix=_SPILL_PREFIX, suffix=".ndjson")
            # An exception between mkstemp and fdopen would leak the raw fd
            # AND the on-disk file. Close the fd if fdopen fails.
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
            resolved = cast("str", path)
            self._fh = open(resolved, "w", encoding="utf-8")
        self.path = resolved
        self.count = 0
        self._claim_ownership()
        # Serialize writes (the file handle is shared) while the (potentially
        # large) json.dumps is offloaded off the event loop.
        self._write_lock = asyncio.Lock()
        global _LAST_SPILL_GC_MONOTONIC
        now_mono = time.monotonic()
        if (
            _LAST_SPILL_GC_MONOTONIC is None
            or now_mono - _LAST_SPILL_GC_MONOTONIC >= _SPILL_GC_MIN_INTERVAL_SECONDS
        ):
            # Cadence gate: the sweep is O(dir) and ran per crawl request; the None
            # stamp guarantees the first construction in a process scans anyway.
            _LAST_SPILL_GC_MONOTONIC = now_mono
            with contextlib.suppress(Exception):
                self._gc_stale_spills(gc_age_seconds)

    def _claim_ownership(self) -> None:
        """Hold this spill's ownership lock for as long as the spill is open.

        Held on the spill's own descriptor, so it cannot be released by any
        path other than closing that descriptor or the process exiting. A
        failure here leaves the spill collectable while it is being written, so
        it is logged rather than swallowed; it needs another process to already
        hold this exact caller-supplied path.
        """
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            logger.warning("spill %s could not be locked for ownership", self.path)

    def _gc_stale_spills(self, max_age: float) -> None:
        """Delete spill files older than ``max_age`` that no live process owns.

        Age alone is not permission to unlink. A spill is reassembled by opening
        its path BY NAME in :meth:`load_ordered`, so removing the directory entry
        out from under a running spill does not cost that worker a local
        checkpoint -- it makes the finished crawl's own output unreadable, after
        the crawl has already been paid for. A whole-site job living past
        ``gc_age_seconds`` is ordinary rather than exotic, and the temp dir is
        shared with every other process on the host, so the sweep asks the
        kernel whether each candidate is still owned
        (:func:`_spill_is_unowned`) rather than consulting a live set only this
        process can see.

        Unlinking an unowned file is still safe on Unix (a reader holding the fd
        keeps the inode) and raises ``PermissionError`` on Windows, which the
        ``except OSError`` below skips.
        """
        now = time.time()
        try:
            names = os.listdir(tempfile.gettempdir())
        except OSError:
            return
        for name in names:
            if not name.startswith(_SPILL_PREFIX):
                continue
            candidate = os.path.join(tempfile.gettempdir(), name)
            if not _spill_is_unowned(candidate):
                continue
            try:
                if now - os.path.getmtime(candidate) > max_age:
                    os.unlink(candidate)
            except OSError:
                continue

    async def write(self, index: int, result: EngineCrawlResult) -> None:
        # Offload the (potentially large) json.dumps to a worker thread so the
        # event loop is never stalled serializing results.
        line = await asyncio.to_thread(json.dumps, {"i": index, "r": to_dict(result)})
        async with self._write_lock:
            self._fh.write(line + "\n")
            self.count += 1

    def load_ordered(self, *, policy: HostPolicy) -> list[EngineCrawlResult]:
        """Reassemble all spilled results in submission order.

        Single pass: each parsed payload is placed directly into its index
        slot — no intermediate tuple list, no O(N log N) sort. The returned
        list still materializes every result: the JSON response contract needs
        the complete ordered set in RAM before the first byte can be written,
        so that materialization is the caller's to pay (``_crawl_request_impl``
        only offloads the file scan and the per-line parse off the event loop).
        Streaming consumers are the documented escape hatch if the full-set
        materialization ever becomes a constraint.

        Each line is rebuilt through :func:`app.models.from_wire`, so the spill
        hands back real :class:`EngineCrawlResult` objects rather than raw dicts
        that every consumer then had to read by key against a hand-declared
        shape. That is what removed the second spelling of a result's key set:
        the class is the declaration, ``to_dict`` and ``from_wire`` are the two
        directions derived from it, and a field the serializer stopped writing
        raises here instead of arriving on the wire as an absent value.
        """
        self._fh.flush()
        slots: dict[int, EngineCrawlResult] = {}
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                slots[entry["i"]] = from_wire(EngineCrawlResult, entry["r"], policy=policy)
        return [slots[key] for key in sorted(slots)]

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._fh.close()
        # The descriptor closed, so the lock released and the sweep can no longer
        # race this cleanup; a temp-path spill drops its file on a happy-path close.
        if getattr(self, "_own", False):
            try:
                os.unlink(self.path)
            except OSError:
                pass


def _set_cookie_namespace(policy: CrawlPolicy):
    from app.transport.cookie_jar import COOKIE_NAMESPACE
    from app.transport.request_profile import cookie_namespace_for

    return COOKIE_NAMESPACE.set(cookie_namespace_for(policy.request_profile))


def _reset_cookie_namespace(token: Any) -> None:
    from app.transport.cookie_jar import COOKIE_NAMESPACE

    COOKIE_NAMESPACE.reset(token)


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
    fetch_and_decode: FetchAndDecode | None = None,
    resource_gate: ResourceGate | None = None,
) -> ProducerEnvelope:
    active_settings = settings or CrawlerSettings.from_mapping()
    host_policy = HostPolicy.from_settings(active_settings)
    # One batch cap -- the same gate the async endpoint uses before accepting, so
    # sync and async cannot drift; a forwarded batch skips re-derivation, not the cap.
    urls = checked_payload_urls(
        payload,
        active_settings.max_batch_size,
        urls=urls,
        host_policy=host_policy,
    )
    policy = payload_policy(payload, PolicyTier.from_settings(active_settings))
    fetcher = http_fetcher or default_http_fetcher
    # A binding of the one fetcher, not a second entry point: the impersonating
    # strategy takes the same callable with the cffi transport preferred.
    cffi = cffi_fetcher or impersonating_fetcher()
    shutdown_event = shutdown_event or asyncio.Event()
    # Bind the cookie namespace for this crawl's lifetime so concurrent tenants
    # cannot leak through the shared jar; "" keeps single-tenant callers unchanged.
    _cookie_token = _set_cookie_namespace(policy)
    try:
        state = _PipelineState(
            payload=payload,
            settings=active_settings,
            urls=urls,
            policy=policy,
            fetcher=fetcher,
            cffi=cffi,
            host_policy=host_policy,
            shutdown_event=shutdown_event,
            browser_worker=browser_worker,
            cache=cache,
            content_cache=content_cache,
            webhook_url=webhook_url,
            spill_path=spill_path,
            dependencies=_PipelineDependencies(
                get_content_cache=_get_shared_content_cache,
                make_spill=_ResultSpill,
                compile_scope_regex_safe=compile_scope_pattern,
                stale_seeds=_stale_seeds,
                fetch_and_decode=fetch_and_decode,
                shared_http_cache=_SHARED_HTTP_CACHE,
                resource_gate=resource_gate if resource_gate is not None else _resource_gate,
            ),
        )
        return await run_crawl_request(state)
    finally:
        _reset_cookie_namespace(_cookie_token)


def reset_process_state() -> None:
    """Rebuild every process-wide cache the engine shares across crawls.

    Three things, reset together on purpose: the robots cache, the shared HTTP
    cache, and the resource gate. The gate memoizes a CPU-quota reading and CPU
    sampling state, so a test that writes a fake cgroup file after the gate has
    sampled once is served a stale reading and concludes the gate ignores the file.
    Dropping the cache but keeping the gate hides that; keeping both but not robots
    serves one origin's old policy body. They are one unit of crawl lifetime, so
    they are reset as one.
    """
    robots_cache.clear()
    global _SHARED_HTTP_CACHE, _resource_gate
    _SHARED_HTTP_CACHE = HttpCache()
    _resource_gate = ResourceGate()


def has_process_state() -> bool:
    """Whether either shared cache still holds an entry.

    The resource gate is reset here too but is not part of the probe, and the
    asymmetry is deliberate rather than an omission. A fresh gate is not *empty*:
    it is unsampled, and nothing in it records whether it has sampled, so there is
    no predicate to ask. Inventing one -- a flag set on first read -- would add
    state to the module in order to report on the state of a different module; the
    memoized reading it worries about lives in ``cgroup_sense``, which is a
    ``ProcessStateOwner`` of its own and is asked directly.
    """
    # ``_SHARED_HTTP_CACHE`` defines no ``__len__``, so bool() is constant True;
    # read the entry mapping instead, or the probe reports state after every reset.
    return bool(robots_cache) or bool(_SHARED_HTTP_CACHE._data)
