"""Crawl execution engine: owns the policy-driven crawl orchestration --
payload/URL/policy parsing, the per-URL strategy chain (HTTP then browser
fallback), worker dispatch, and final result assembly. The single entry point crawlutron.main calls.
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
from collections.abc import Iterable, Mapping
from contextvars import Token
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol, TypedDict, cast

from crawlutron.admission.admission_links import compile_scope_pattern
from crawlutron.admission.payload_helpers import checked_payload_urls, payload_policy
from crawlutron.admission.policy import PolicyTier
from crawlutron.config.env import get_env_int
from crawlutron.config.settings import CrawlerSettings
from crawlutron.models.crawl_request import CrawlRequest
from crawlutron.models.crawl_result import EngineCrawlResult
from crawlutron.models.host_policy import HostPolicy
from crawlutron.models.json_value import JsonValue
from crawlutron.models.serialization import from_wire, to_dict
from crawlutron.orchestration.request_pipeline import (
    PipelineDependencies,
    PipelineState,
    run_crawl_request,
)
from crawlutron.runtime import process_state
from crawlutron.runtime.pressure_gates import ResourceGate
from crawlutron.transport import robots as _robots_policy
from crawlutron.transport.cache import HttpCache
from crawlutron.transport.content_cache import ContentCache
from crawlutron.transport.fetchers import default_http_fetcher, impersonating_fetcher
from crawlutron.transport.robots import FetchAndDecode

if TYPE_CHECKING:
    from crawlutron.models.crawl_policy import CrawlPolicy
    from crawlutron.models.crawl_worker import CrawlWorker
    from crawlutron.models.http_fetcher import HttpFetcher
    from crawlutron.models.normalized_url import NormalizedUrl
    from crawlutron.models.result_record import ProducerEnvelope

logger = logging.getLogger(__name__)

robots_cache = _robots_policy.robots_cache
RobotsMatcher = _robots_policy.RobotsMatcher
load_robots_rules = _robots_policy.load_robots_rules
cached_sitemaps = _robots_policy.cached_sitemaps

# Read the gate via resource_gate(): do not capture the reference before the reset swaps it.
_resource_gate = ResourceGate()

# Shared conditional cache so repeated crawls can leverage 304 responses.
_SHARED_HTTP_CACHE = HttpCache()

# Lazy, disk-backed content cache. Created on first use so importing
# this module has no filesystem side effects (tests can pass their own).
_SHARED_CONTENT_CACHE: ContentCache | None = None


def resource_gate() -> ResourceGate:
    """The process admission gate the engine reserves against."""
    return _resource_gate


def get_shared_content_cache() -> ContentCache:
    global _SHARED_CONTENT_CACHE
    if _SHARED_CONTENT_CACHE is None:
        settings = CrawlerSettings.from_mapping()
        _SHARED_CONTENT_CACHE = ContentCache(
            cache_dir=settings.cache_dir,
            default_ttl=settings.cache_ttl_seconds,
        )
    return _SHARED_CONTENT_CACHE


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
    # Lazy import avoids a module cycle (engine <-> http).
    from crawlutron.transport.content_cache_key import build_content_cache_key
    from crawlutron.transport.request_profile import headers_for_strategy

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


SPILL_PREFIX = "bc-spill-"

# A spill is owned while any process holds an advisory lock on it -- a kernel
# fact, not memory (see :func:`spill_is_unowned`). Sweep: one scan per interval.
SPILL_GC_MIN_INTERVAL_SECONDS = 300.0
spill_gc_last_run: float | None = None


def spill_is_unowned(path: str) -> bool:
    """True when no live process holds ``path``'s ownership lock -- taking the lock
    IS the test: the kernel grants it only when no other open file description
    holds it, tying ownership to the holder's lifetime. An unopenable path reports owned.
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


class _SpillLine(TypedDict):
    """One NDJSON line the writer (``write``) emitted: its submission index and the
    result's wire form. The declared shape the loader checks, not guesses."""

    i: int
    r: dict[str, JsonValue]


def _spill_encode(index: int, result: EngineCrawlResult) -> str:
    """Build one spill line -- ``to_dict`` plus ``json.dumps`` -- as ONE offloaded
    unit: the worker lambda runs both, so neither the multi-MB dict walk nor the
    dump lands on the event loop."""
    return json.dumps({"i": index, "r": to_dict(result)})


def _spill_line(line: str) -> _SpillLine:
    """One spill line, checked: a line this writer did not write (wrong keys, a
    non-int index) raises instead of slipping a corrupt record into the ordering."""
    entry: JsonValue = json.loads(line)
    shape_ok = (
        isinstance(entry, dict) and isinstance(entry.get("i"), int) and isinstance(entry.get("r"), dict)
    )
    if not shape_ok:
        raise TypeError(f"spill line is not a {_SpillLine.__name__}: {line[:80]!r}")
    return cast(_SpillLine, entry)


class ResultSpill:
    """Append-only NDJSON spill of page results: each page hits disk when its batch
    finishes, so peak RSS scales with batch size and RAM keeps only the running
    count; the response is reassembled in index order, auto-created files deleted on close.
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
            fd, resolved = tempfile.mkstemp(prefix=SPILL_PREFIX, suffix=".ndjson")
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
        global spill_gc_last_run
        now_mono = time.monotonic()
        if spill_gc_last_run is None or now_mono - spill_gc_last_run >= SPILL_GC_MIN_INTERVAL_SECONDS:
            # Cadence gate: the sweep is O(dir) and ran per crawl request; the None
            # stamp guarantees the first construction in a process scans anyway.
            spill_gc_last_run = now_mono
            with contextlib.suppress(Exception):
                self.gc_stale_spills(gc_age_seconds)

    def _claim_ownership(self) -> None:
        """Hold this spill's ownership lock for as long as the spill is open.
        Held on the spill's own descriptor, so only closing it or process exit
        releases it. Failure is FATAL (#C7): an unlocked spill is exactly the file
        the sweep's own-lock probe may collect after gc_age_seconds -- a long crawl
        would lose its result. The half-built spill is reaped before raising.
        """
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            with contextlib.suppress(Exception):
                self._fh.close()
            if self._own:
                with contextlib.suppress(OSError):
                    os.unlink(self.path)
            raise OSError(
                f"spill {self.path} could not be owned; refusing an ownable-by-anyone spill"
            ) from error

    def gc_stale_spills(self, max_age: float) -> None:
        """Delete spill files older than ``max_age`` that no live process owns.
        Age alone is not permission: a spill is reassembled BY NAME in load_ordered,
        so the sweep asks the kernel (spill_is_unowned), never a live set only this process sees.
        """
        now = time.time()
        try:
            names = os.listdir(tempfile.gettempdir())
        except OSError:
            return
        for name in names:
            if not name.startswith(SPILL_PREFIX):
                continue
            candidate = os.path.join(tempfile.gettempdir(), name)
            if not spill_is_unowned(candidate):
                continue
            try:
                if now - os.path.getmtime(candidate) > max_age:
                    os.unlink(candidate)
            except OSError:
                continue

    async def write(self, index: int, result: EngineCrawlResult) -> None:
        # Offload the WHOLE line build -- to_dict walking a multi-MB result plus
        # json.dumps of it -- so neither step ever runs on the event loop.
        line = await asyncio.to_thread(_spill_encode, index, result)
        async with self._write_lock:
            self._fh.write(line + "\n")
            self.count += 1

    def flush(self) -> None:
        """The public flush door: load_ordered flushes internally; tests and
        tooling reading the file mid-flight use the same one handle."""
        self._fh.flush()

    def load_ordered(self, *, policy: HostPolicy) -> list[EngineCrawlResult]:
        """Reassemble all spilled results in submission order, one pass, via ``from_wire``;
        RAM is the materialized set, bounded by the batch wire cap (500) or max_pages (2000).
        A corrupt line (a crash's partial buffered write, binary corruption included) is
        SKIPPED and logged, never fatal (#C8): one bad record must not erase the N-1 results."""
        self._fh.flush()
        slots: dict[int, EngineCrawlResult] = {}
        # Per-LINE decoding: a text handle decodes in buffered chunks, so one
        # bad byte poisons the whole chunk -- every GOOD line before it dies
        # too (observed live, adversarial pass 5). Decode each line alone.
        with open(self.path, "rb") as fh:
            for raw in fh:
                try:
                    line = raw.decode("utf-8")
                except UnicodeDecodeError:
                    logger.warning("skipping corrupt spill line: undecodable bytes: %.40r", raw)
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = _spill_line(line)  # parsed once per line (N1)
                except (json.JSONDecodeError, TypeError):
                    logger.warning("skipping corrupt spill line: %.80r", line)
                    continue
                # OUTSIDE the skip-guard: a record the WIRE refuses is a writer
                # lie, not a crash artifact -- it raises, never silently skips.
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
    from crawlutron.transport.cookie_jar import COOKIE_NAMESPACE
    from crawlutron.transport.request_profile import cookie_namespace_for

    return COOKIE_NAMESPACE.set(cookie_namespace_for(policy.request_profile))


def _reset_cookie_namespace(token: Token[str]) -> None:
    from crawlutron.transport.cookie_jar import COOKIE_NAMESPACE

    COOKIE_NAMESPACE.reset(token)


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
    webhook_url: str | None = None,
    shutdown_event: asyncio.Event | None = None,
    spill_path: str | None = None,
    fetch_and_decode: FetchAndDecode | None = None,
    resource_gate: ResourceGate | None = None,
) -> ProducerEnvelope:
    """One typed crawl through every entry point. ``payload`` is a model; a wire
    mapping is validated once to it here (the seam's single coercion), so every
    downstream read is typed -- ``payload.policy``, ``payload.url``, never a dict.
    """
    if not isinstance(payload, CrawlRequest):
        payload = CrawlRequest.model_validate(dict(payload))
    active_settings = settings or CrawlerSettings.from_mapping()
    host_policy = HostPolicy.from_settings(active_settings)
    # One batch cap -- the same gate the async endpoint uses before accepting, so
    # sync and async cannot drift. Pairs keep the one admitted NormalizedUrl.
    url_pairs = checked_payload_urls(
        payload, active_settings.max_batch_size, url_pairs=url_pairs, host_policy=host_policy
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
            spill_path=spill_path,
            dependencies=_pipeline_dependencies(fetch_and_decode, resource_gate),
        )
        return await run_crawl_request(state)
    finally:
        _reset_cookie_namespace(_cookie_token)


def _pipeline_dependencies(
    fetch_and_decode: FetchAndDecode | None,
    resource_gate: ResourceGate | None,
) -> PipelineDependencies:
    """The process-wide dependencies a crawl binds: shared caches, spill, gate."""
    return PipelineDependencies(
        get_content_cache=get_shared_content_cache,
        make_spill=ResultSpill,
        compile_scope_regex_safe=compile_scope_pattern,
        stale_seeds=stale_seeds,
        fetch_and_decode=fetch_and_decode,
        shared_http_cache=_SHARED_HTTP_CACHE,
        resource_gate=resource_gate if resource_gate is not None else _resource_gate,
    )


def _reset() -> None:
    """Rebuild every process-wide cache the engine shares across crawls."""
    robots_cache.clear()
    global _SHARED_HTTP_CACHE, _resource_gate
    _SHARED_HTTP_CACHE = HttpCache()
    # The shared admission gate, reset with the caches; see :func:`resource_gate`.
    _resource_gate = ResourceGate()
    # The lazy content cache is part of the same lifetime: empty it when built.
    if _SHARED_CONTENT_CACHE is not None:
        _SHARED_CONTENT_CACHE.clear()
    # A reset starts a new process lifetime, so the next spill must scan again.
    global spill_gc_last_run
    spill_gc_last_run = None


def _has() -> bool:
    """Whether any shared cache still holds an entry."""
    # ``_SHARED_HTTP_CACHE`` defines no ``__len__``: bool() is constant True, so
    # read the entry mapping instead, or the probe reports state after every reset.
    content_holds = _SHARED_CONTENT_CACHE is not None and bool(_SHARED_CONTENT_CACHE._index)
    return bool(robots_cache) or bool(_SHARED_HTTP_CACHE._data) or content_holds


process_state.register(__name__, _reset, _has)
