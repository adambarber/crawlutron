"""Crawl execution engine.

Owns the policy-driven crawl orchestration: payload/URL/policy parsing,
the per-URL strategy chain (HTTP -> browser fallback), worker dispatch, and
the final result assembly. This module is the single entry point that the
FastAPI layer (``app.main``) calls into.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin, urlparse

from app.api_models import POLICY_SCHEMA_VERSION
from app.constants import (
    BROWSER_BLOCKED_CLASSIFICATIONS,
    BROWSER_STRATEGIES,
    DEFAULT_EXTRACTION,
    DEFAULT_STRATEGIES,
    DEFAULT_TOTAL_URL_BUDGET_SECONDS,
    ENGINE_BATCH_CONCURRENCY_MULTIPLIER,
    EXTRACTION_DOCUMENT,
    USER_AGENT_TOKEN,
    EXTRACTION_METHOD_DOCUMENT,
    EXTRACTION_METHOD_DOM_TEXT,
    EXTRACTION_METHOD_TRAFILATURA,
    EXTRACTION_TRAFILATURA,
    HTTP_BLOCKED_CLASSIFICATIONS,
    HTTP_CFFI_IMPERSONATE,
    HTTP_STRATEGIES,
)
from app.memory_pressure import read_memory_stats
from app.resource_pressure import ResourceGate, WeightedAdmission, _build_admission_policy

logger = logging.getLogger(__name__)

_resource_gate = ResourceGate()
from app.crawl.cache import HttpCache
from app.crawl.content_cache import ContentCache
from app.crawl.extraction import (
    _detect_pagination,
    _extract_discovered_links,
    _extract_document,
    _extract_html,
    _html_diagnostics,
    _parse_html_tree,
    extract_content,
    extract_js_endpoints,
)
from app.crawl.fetch_helpers import _fetch_and_decode, fetch_sitemap_urls
from app.crawl.payload_helpers import _payload_policy, _payload_urls
from app.crawl.recursive import RECURSIVE_RAMP_MAX, _spill_write, run_recursive_crawl
from app.crawl.result_helpers import (
    _failed_engine_result,
    _skipped_engine_result,
)
from app.crawl.strategies.http import (
    _default_cffi_fetcher,
    _default_http_fetcher,
    _http_strategy_result,
    _quality_score,
)
from app.models import (
    CrawlJob,
    CrawlResult,
    CrawlStrategyAttempt,
    CrawlTask,
    CrawlWorker,
    EngineCrawlResult,
    HttpFetcher,
    RenderOptions,
    to_dict,
)
from app.resource_pressure import derive_browser_pool_size
from app.scheduler import DomainScheduler
from app.settings import CrawlerSettings
from app.validation import normalize_public_http_url, registrable_domain

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





async def _call_worker(worker: CrawlWorker, job: CrawlJob) -> CrawlResult:
    result = worker(job)
    if inspect.iscoroutine(result):
        return await result
    return await asyncio.to_thread(worker, job)



# A single user-supplied scope_regex is applied to every discovered link.
# Bound the work and refuse pathological patterns (ReDoS guard).
_MAX_SCOPE_PATTERN_LEN = 256
_MAX_SCOPE_QUANTIFIERS = 10
_MAX_SCOPE_LINKS = 5000
# A group immediately followed by a quantifier that itself contains a quantifier
# or alternation is the classic catastrophic-backtracking shape (e.g. "(a+)+$").
_SCOPE_GROUP_QUANT = re.compile(r"\((?:[^()]|\([^()]*\))*\)\s*[+*?{]")


def _compile_scope_regex_safe(scope_regex: str | None):
    """Compile a user-supplied scope regex, refusing pathological patterns.

    A malicious or careless ``scope_regex`` (e.g. ``(a+)+$``) can trigger
    catastrophic backtracking and hang the request. We reject patterns that are
    too long or contain a quantified group (nested quantifier / alternation),
    which are the classic ReDoS vectors. Invalid patterns (``re.error``) are
    also rejected, preserving the previous safe-fallback behavior.
    """
    if not scope_regex:
        return None
    if len(scope_regex) > _MAX_SCOPE_PATTERN_LEN:
        return None
    if _SCOPE_GROUP_QUANT.search(scope_regex):
        return None
    try:
        compiled = re.compile(scope_regex)
    except re.error:
        return None
    # Ungrouped quantifier chains (e.g. "a?a?a?..." repeated 20+ times) defeat
    # the grouped-quantifier rule above while still creating exponential
    # ambiguity on failed matches. Bound the number of quantifier tokens:
    # legitimate scope regexes need only a handful.
    # Brace forms include leading-comma variants ({,n}) which are valid Python
    # regex quantifiers -- verified event-loop-hang bypass if uncounted.
    quantifier_tokens = len(re.findall(r"(?<!\\)(?:[*+?]|\{\d*(?:,\d*)?\})", scope_regex))
    if quantifier_tokens > _MAX_SCOPE_QUANTIFIERS:
        return None
    return compiled



async def _browser_strategy_result(
    *,
    original_url: str,
    normalized_url: str,
    strategy: str,
    policy: dict[str, Any],
    browser_worker: CrawlWorker,
) -> EngineCrawlResult:
    options = RenderOptions(
        timeout_seconds=policy["limits"]["timeout_seconds"],
        max_retries=policy["limits"]["max_retries"],
        respect_robots=True,
        include_html=True,
        include_text=True,
        browser_profile=policy.get("browser_profile") or {},
    )
    job = CrawlJob(index=0, url=normalized_url, domain=registrable_domain(normalized_url), options=options)
    result = await _call_worker(browser_worker, job)
    extraction = policy.get("extraction", DEFAULT_EXTRACTION)
    content = result.html if result.html else result.text
    if content:
        if extraction == EXTRACTION_DOCUMENT:
            extracted = await asyncio.to_thread(
                _extract_document,
                content.encode("utf-8", "replace") if isinstance(content, str) else content,
                result.final_url,
            )
            text = extracted.text or ""
            markdown = extracted.markdown
            extraction_method = EXTRACTION_METHOD_DOCUMENT
        elif extraction == EXTRACTION_TRAFILATURA:
            extracted = _extract_html(content, result.final_url, css_selector=policy.get("css_selector"), xpath=policy.get("xpath"))
            text = extracted.text or ""
            markdown = extracted.markdown
            extraction_method = EXTRACTION_METHOD_TRAFILATURA
        else:
            extracted = await asyncio.to_thread(extract_content, content, None, result.final_url, css_selector=policy.get("css_selector"), xpath=policy.get("xpath"))
            text = extracted.text or ""
            markdown = extracted.markdown
            extraction_method = result.metadata.get("extraction_method") or EXTRACTION_METHOD_DOM_TEXT
    else:
        text = ""
        markdown = None
        extraction_method = result.metadata.get("extraction_method") or EXTRACTION_METHOD_DOM_TEXT

    # Surface browser-captured dynamic discovery (XHR/fetch) into diagnostics.
    # #11: parse the page HTML once and reuse the single lxml tree for the
    # frames/forms diagnostics AND the discovered-link extraction instead of
    # reparsing the same bytes in each pass.
    tree = _parse_html_tree(result.html)
    diagnostics: dict[str, Any] = {
        "browser_profile": policy.get("browser_profile") or {},
        "runtime": {"elapsed_ms": result.elapsed_ms, "final_url": result.final_url},
        "content_metrics": {"visible_text_length": len((text or "").strip()), "html_length": len((result.html or "").strip())},
        **_html_diagnostics(result.html, result.final_url, tree=tree),
    }
    intercepted = result.metadata.get("intercepted_xhr")
    if intercepted:
        diagnostics["intercepted_xhr"] = intercepted
    screenshot = result.metadata.get("screenshot")
    if screenshot:
        diagnostics["screenshot"] = screenshot

    quality = _quality_score(result.status, result.classification, text, result.html, None)
    discovered_links, discovered_link_count, discovered_links_truncated = (
        _extract_discovered_links(result.html, result.final_url, max_links=policy["limits"]["max_discovered_links"], tree=tree) if result.status == "success" else ([], 0, False)
    )
    next_page_url = _detect_pagination(result.html, result.final_url)
    if next_page_url:
        diagnostics["next_page_url"] = next_page_url
    diagnostics["js_endpoints"] = extract_js_endpoints(result.html, result.final_url)
    attempt = CrawlStrategyAttempt(
        strategy=strategy,  # type: ignore[arg-type]
        status=result.status,
        classification=result.classification,
        http_status=None,
        content_type=None,
        elapsed_ms=result.elapsed_ms,
        error=result.error,
    )
    return EngineCrawlResult(
        original_url=original_url,
        normalized_url=normalized_url,
        final_url=result.final_url,
        status=result.status,
        classification=result.classification,
        selected_strategy=strategy if result.status == "success" else None,  # type: ignore[arg-type]
        attempted_strategies=[attempt],
        http_status=None,
        headers={},
        content_type=None,
        html=result.html,
        text=text,
        markdown=markdown,
        extraction_method=extraction_method,
        quality_score=quality,
        classification_evidence=[],
        diagnostics=diagnostics,
        discovered_link_count=discovered_link_count,
        discovered_links_truncated=discovered_links_truncated,
        discovered_links=discovered_links,
        error=result.error,
        next_page_url=next_page_url,
    )



# Block/challenge classifications that should trigger strategy auto-escalation (#7).
BLOCKED_CLASSIFICATIONS = HTTP_BLOCKED_CLASSIFICATIONS | BROWSER_BLOCKED_CLASSIFICATIONS

async def _execute_crawl_chain(
    *,
    original_url: str,
    normalized_url: str,
    policy: dict[str, Any],
    http_fetcher: HttpFetcher,
    cffi_fetcher: HttpFetcher,
    browser_worker: CrawlWorker | None,
    cache: HttpCache | None = None,
    content_cache: ContentCache | None = None,
    scheduler: DomainScheduler | None = None,
    domain: str = "",
    caller_validators: dict[str, str] | None = None,
    robots_sitemaps: list[str] | None = None,
) -> EngineCrawlResult:
    attempts: list[CrawlStrategyAttempt] = []
    last_result: EngineCrawlResult | None = None
    cffi_was_blocked = False
    cffi_retry_done = False

    # Strategy pinning: if this domain has a pinned strategy from a prior
    # successful fetch in this job, start the chain at that strategy (skip
    # cheaper strategies that already failed for this domain).
    strategies = policy["strategies"]
    if scheduler is not None:
        pinned = scheduler.get_pinned_strategy(domain)
        if pinned is not None and pinned in strategies:
            start_idx = strategies.index(pinned)
            strategies = strategies[start_idx:]

    for strategy in strategies:
        if strategy in HTTP_STRATEGIES:
            strategy_fetcher = cffi_fetcher if strategy == HTTP_CFFI_IMPERSONATE else http_fetcher
            result = await _http_strategy_result(
                original_url=original_url,
                normalized_url=normalized_url,
                strategy=strategy,
                policy=policy,
                http_fetcher=strategy_fetcher,
                cache=cache,
                content_cache=content_cache,
                caller_validators=caller_validators,
                robots_sitemaps=robots_sitemaps,
            )
        elif strategy in BROWSER_STRATEGIES:
            if browser_worker is None:
                result = _failed_engine_result(
                    original_url=original_url,
                    normalized_url=normalized_url,
                    strategy=strategy,
                    classification="browser_crash",
                    error="browser_worker_unavailable",
                    diagnostics={
                        "browser_profile": policy.get("browser_profile") or {},
                        "runtime": {"error": "browser_worker_unavailable"},
                    },
                )
            else:
                result = await _browser_strategy_result(
                    original_url=original_url,
                    normalized_url=normalized_url,
                    strategy=strategy,
                    policy=policy,
                    browser_worker=browser_worker,
                )
        else:
            result = _failed_engine_result(
                original_url=original_url,
                normalized_url=normalized_url,
                strategy=strategy,
                classification="unknown_error",
                error="unsupported_strategy",
            )
        attempts.extend(result.attempted_strategies)
        last_result = result
        if strategy == HTTP_CFFI_IMPERSONATE and result.status != "success":
            cffi_was_blocked = True
        if result.classification == "rate_limited" and scheduler is not None:
            cooldown = result.diagnostics.get("cooldown") or {}
            retry_after = cooldown.get("retry_after_seconds")
            if retry_after is not None:
                scheduler.set_domain_cooldown(domain, float(retry_after))
        if result.classification in BLOCKED_CLASSIFICATIONS:
            # A challenge/block page (CAPTCHA, Cloudflare/WAF, login wall, etc.) is not a
            # usable result. Auto-escalate to the next strategy in the chain (e.g. direct
            # HTTP -> curl_cffi TLS impersonation -> headless browser) before giving up (#7).
            # The final fallback keeps the last (blocked) result.
            continue
        # not_modified is terminal: the caller's validators proved freshness, so
        # escalating to the next strategy would just re-fetch the page.
        # Cookie bootstrap: after a successful render on a domain where cffi
        # was blocked, retry cffi once with browser-minted session cookies.
        from app.crawl.strategies.browser import get_browser_cookies_for
        if (
            result.status == "success"
            and result.classification == "rendered"
            and strategy in BROWSER_STRATEGIES
            and cffi_was_blocked
            and not cffi_retry_done
        ):
            cffi_retry_done = True
            cookie_header = get_browser_cookies_for(registrable_domain(original_url))
            if cookie_header:
                retry_policy = dict(policy)
                retry_headers = dict(retry_policy.get("headers") or {})
                retry_headers["Cookie"] = cookie_header
                retry_policy["headers"] = retry_headers
                retry_result = await _http_strategy_result(
                    original_url=original_url, normalized_url=normalized_url,
                    strategy=HTTP_CFFI_IMPERSONATE, policy=retry_policy,
                    http_fetcher=cffi_fetcher, cache=cache,
                    content_cache=content_cache, caller_validators=caller_validators,
                    robots_sitemaps=robots_sitemaps,
                )
                attempts.extend(retry_result.attempted_strategies)
                if retry_result.status == "success" and retry_result.quality_score > 0:
                    return replace(retry_result, attempted_strategies=attempts)
                last_result = result

        if result.status == "success" and (result.quality_score > 0 or result.classification == "not_modified"):
            # Pin the successful strategy for this domain so subsequent fetches
            # in the same job start here instead of re-climbing the ladder.
            if scheduler is not None:
                scheduler.set_pinned_strategy(domain, strategy)
            return replace(result, attempted_strategies=attempts)
    assert last_result is not None
    return replace(last_result, attempted_strategies=attempts)


@functools.lru_cache(maxsize=1024)
def _robots_pattern_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a robots.txt path pattern to a regex.

    Per RFC 9309 / Google's interpretation: ``*`` matches any sequence of
    characters and ``$`` anchors the end of the path. Patterns are matched as
    path prefixes (left-anchored). An empty pattern matches everything.
    """
    if pattern == "":
        return re.compile(r".*")
    parts: list[str] = ["^"]
    last = len(pattern) - 1
    for idx, ch in enumerate(pattern):
        if ch == "*":
            parts.append(".*")
        elif ch == "$" and idx == last:
            parts.append("$")
        else:
            parts.append(re.escape(ch))
    return re.compile("".join(parts))


ROBOTS_CACHE_MAX_ENTRIES = 1000

# Module-level robots cache shared across all crawl_request calls in the
# process (#9). Previously each crawl_request built its own cache, so two
# separate API calls to the same domain each re-fetched /robots.txt. Promoting
# it here lets the TTL cache be reused across requests; it is bounded by
# ROBOTS_CACHE_MAX_ENTRIES via _cap_robots_cache.
robots_cache: dict[str, tuple[float, list[tuple[bool, str]], list[str], float | None]] = {}
# Concurrent crawl_requests can race on robots_cache (the dict is mutated from
# the async event loop, but _cap_robots_cache is also reachable from worker
# threads when paired with the to_thread offload). A plain dict.get/dict[]
# sequence is not atomic under concurrent mutation (adversarial review
# F-thread-safety); the lock makes the read-modify-write sequence atomic.
_ROBOTS_CACHE_LOCK = threading.Lock()


def _cap_robots_cache(cache: dict, max_size: int) -> None:
    """Evict oldest entries (insertion order) once ``cache`` exceeds ``max_size``.

    The robots cache lives for the duration of a single crawl_request and is
    normally trimmed by TTL, but a crawl that discovers thousands of unique
    (still-fresh) domains would otherwise grow it without bound. This bounds
    memory by evicting the least-recently-inserted origins (LRU by insertion).
    """
    while len(cache) > max_size:
        cache.pop(next(iter(cache)))


def _robots_allows(target_url: str, rules: list[tuple[bool, str]]) -> bool:
    """Return True if ``target_url`` is allowed by the robots ``rules``.

    Implements RFC 9309 path matching: ``*`` wildcards, ``$`` end anchors, and
    the longest-matching-rule-wins precedence. An empty ``Disallow:`` value
    means "allow everything" (no restriction) and overrides other rules.
    """
    parsed = urlparse(target_url)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    # RFC 9309: an empty Disallow value permits all URLs for the group.
    if any((not is_allow) and pattern == "" for is_allow, pattern in rules):
        return True

    allowed = True
    best_len = -1
    for is_allow, pattern in rules:
        if not pattern:
            continue
        if _robots_pattern_to_regex(pattern).match(path):
            plen = len(pattern)
            if plen > best_len:
                best_len = plen
                allowed = is_allow
            elif plen == best_len and is_allow:
                # On a tie, allow wins (Google's robots.txt guidance).
                allowed = True
    return allowed


def _parse_lastmod(lastmod: str | None) -> float | None:
    """Parse a sitemap <lastmod> (ISO-8601 or RFC-822) into epoch seconds."""
    if not lastmod:
        return None
    s = lastmod.strip()
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(s)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt.timestamp()
    except (TypeError, ValueError):
        pass
    iso = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.timestamp()
    except ValueError:
        return None


def _seed_is_fresh(
    url: str, lastmod: str | None, content_cache: Any, policy: dict[str, Any]
) -> bool:
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
    from app.crawl.strategies.http import _content_cache_key, _headers_for_strategy

    # Default to the real strategy chain (L5): the old ["HTTP"] placeholder
    # is not a strategy, so its cache keys could never match stored entries
    # and freshness was never detected through this path.
    for strategy in policy.get("strategies", DEFAULT_STRATEGIES):
        key = _content_cache_key(
            url, strategy, _headers_for_strategy(strategy, policy, url),
            extraction, css, xp,
        )
        cached_at = content_cache.cached_at_of(key)
        if cached_at is not None and lastmod_ts <= cached_at:
            return True
    return False


_SPILL_PREFIX = "bc-spill-"


class _ResultSpill:
    """Append-only NDJSON spill of page results (#2-stream).

    Each completed page is serialized to disk the moment its batch finishes, so
    peak RSS scales with batch size instead of total pages. RAM keeps only the
    running count; the response is reassembled by reading the file back in
    index order. Auto-created files are deleted on close; stale spills from
    killed runs are garbage-collected when the next crawl starts.
    """

    def __init__(self, path: str | None = None, *, gc_age_seconds: float = 3600):
        self._own = path is None
        if self._own:
            fd, path = tempfile.mkstemp(prefix=_SPILL_PREFIX, suffix=".ndjson")
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
                    os.unlink(path)
                except OSError:
                    pass
                raise
        else:
            self._fh = open(path, "w", encoding="utf-8")
        self.path = path
        self.count = 0
        # Serialize writes (the file handle is shared) while the (potentially
        # large) json.dumps is offloaded off the event loop (adversarial #1.3).
        self._write_lock = asyncio.Lock()
        try:
            self._gc_stale_spills(gc_age_seconds)
        except Exception:
            # GC failure must not abort the spill (adversarial #result-spill
            # review). The next spill instance will retry.
            pass

    def _gc_stale_spills(self, max_age: float) -> None:
        """Delete spill files older than ``max_age`` from the system temp dir.

        Race window (low risk in practice): if another worker still holds the
        file open via ``os.fdopen`` (which we own for the lifetime of the
        ``_ResultSpill`` instance), the unlink will remove the directory entry
        but the inode stays alive until the last fd closes. Linux/Unix
        semantics mean the file is fully reclaimed on close; on Windows the
        unlink would fail with PermissionError and we silently skip it. No
        data loss occurs because the file is owned by a single worker at a
        time and the worst-case is "delete a still-active spill" which only
        loses that worker's local checkpoint, not the active crawl result.
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
        """Reassemble all spilled results in submission order."""
        self._fh.flush()
        rows: list[tuple[int, dict[str, Any]]] = []
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    entry = json.loads(line)
                    rows.append((entry["i"], entry["r"]))
        rows.sort(key=lambda pair: pair[0])
        return [payload for _, payload in rows]

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass
        # Auto-cleanup of owned files: when this spill was created without
        # an explicit path (caller-owned spills retain their file), drop the
        # backing temp file so a happy-path close never orphans it.
        if getattr(self, "_own", False):
            try:
                os.unlink(self.path)
            except OSError:
                pass





def _parse_robots_crawl_delay(text: str) -> float | None:
    """Extract the Crawl-delay (seconds) for our user-agent group, if any.

    Mirrors :func:`_parse_robots_rules` group selection (the most-specific
    group matching our product token wins, else the ``*`` group). A host's
    Crawl-delay is a politeness floor the scheduler must honor (adversarial
    perf #2.6). Returns None when no group declares one.
    """
    our_token = USER_AGENT_TOKEN
    groups: list[list] = []
    last_agents: list[str] = []
    current: list | None = None
    for raw in text.lstrip("\ufeff").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key == "user-agent":
            if current is not None:
                groups.append(current)
                current = None
            last_agents.append(value.lower())
        elif key == "crawl-delay" and value:
            if current is None:
                current = [list(last_agents), None]
                last_agents = []
            try:
                delay = float(value)
            except ValueError:
                delay = None
            if delay is not None and delay >= 0:
                current[1] = delay
    if current is not None:
        groups.append(current)

    best_delay: float | None = None
    best_len = -1
    star_delay: float | None = None
    for agents, delay in groups:
        for agent in agents:
            if agent == "*":
                if star_delay is None:
                    star_delay = delay
                break
            if agent and agent in our_token and len(agent) > best_len:
                best_len = len(agent)
                best_delay = delay
                break
    if best_delay is not None:
        return best_delay
    return star_delay


async def crawl_request(
    payload: dict[str, Any],
    *,
    settings: CrawlerSettings | None = None,
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
    urls = _payload_urls(payload)
    if len(urls) > active_settings.max_batch_size:
        raise ValueError("batch_size_exceeded")
    policy = _payload_policy(payload, active_settings)
    fetcher = http_fetcher or _default_http_fetcher
    cffi = cffi_fetcher or _default_cffi_fetcher
    shutdown_event = shutdown_event or asyncio.Event()
    # Bind the cookie namespace for the lifetime of this crawl so concurrent
    # multi-tenant calls cannot leak cookies through the shared
    # ``_GLOBAL_COOKIE_JAR``. Defaults to "" (legacy shared behavior) so
    # single-tenant callers and tests see no change (adversarial review
    # F-cookie-cross-contamination).
    from app.crawl.strategies.http import (
        _COOKIE_NAMESPACE,
        cookie_namespace_for,
    )
    _cookie_token = _COOKIE_NAMESPACE.set(cookie_namespace_for(policy.get("request_profile") or {}))
    try:
        return await _crawl_request_impl(
            payload, active_settings, urls, policy, fetcher, cffi, shutdown_event,
            browser_worker=browser_worker, cache=cache, content_cache=content_cache,
            webhook_url=webhook_url, spill_path=spill_path,
        )
    finally:
        _COOKIE_NAMESPACE.reset(_cookie_token)


async def _crawl_request_impl(
    payload: dict[str, Any],
    active_settings: CrawlerSettings,
    urls: list[str],
    policy: dict[str, Any],
    fetcher: Any,
    cffi: Any,
    shutdown_event: asyncio.Event,
    *,
    browser_worker: CrawlWorker | None,
    cache: HttpCache | None,
    content_cache: ContentCache | None,
    webhook_url: str | None,
    spill_path: str | None,
) -> dict[str, Any]:

    jobs: list[CrawlTask] = []
    for index, original_url in enumerate(urls):
        try:
            normalized_url = normalize_public_http_url(original_url)
            domain = registrable_domain(normalized_url)
        except ValueError as error:
            raise ValueError(f"invalid_url: {error}") from error
        jobs.append(CrawlTask(index=index, original_url=original_url, normalized_url=normalized_url, domain=domain))

    # Limits are the same for all jobs; extract once for scheduler config
    limits = policy.get("limits", {})
    scheduler = DomainScheduler(
        # No fixed external concurrency cap: let the recursive dispatcher's
        # self-tuning ramp (RECURSIVE_RAMP_MAX * ENGINE_BATCH_CONCURRENCY_MULTIPLIER
        # = 8 * 2 = 16) size real concurrency to the cgroup budget; the resource
        # gate is the sole authority. This limit mirrors that real ceiling so a
        # single scheduler batch cannot exceed the dispatcher's own ramp bound.
        global_limit=RECURSIVE_RAMP_MAX * ENGINE_BATCH_CONCURRENCY_MULTIPLIER,
        per_domain_limit=active_settings.per_domain_concurrency,
        delay_seconds=active_settings.per_domain_delay_ms / 1000,
        max_rps=float(limits.get("max_rps", 0) or 0),
        adaptive=active_settings.adaptive_throttling,
        adaptive_min_delay=active_settings.adaptive_min_delay,
        adaptive_max_delay=active_settings.adaptive_max_delay,
        adaptive_latency_factor=active_settings.adaptive_latency_factor,
    )

    # Conditional revalidation (freshness): caller-supplied validators keyed by
    # URL (original or normalized form both accepted).
    validators_raw = payload.get("validators") or {}
    validators_by_url: dict[str, dict[str, str]] = {}
    for v_url, v in validators_raw.items():
        entry = {"if_none_match": v.get("if_none_match"), "if_modified_since": v.get("if_modified_since")}
        entry = {k: val for k, val in entry.items() if val}
        if not entry:
            continue
        validators_by_url[v_url] = entry
        try:
            validators_by_url.setdefault(normalize_public_http_url(v_url), entry)
        except ValueError:
            pass

    def _validators_for(job: CrawlTask) -> dict[str, str] | None:
        return (
            validators_by_url.get(job.normalized_url)
            or validators_by_url.get(job.original_url)
        )

    async def run_job(job: CrawlTask) -> tuple[int, EngineCrawlResult]:
        index = job.index
        original_url = job.original_url
        normalized_url = job.normalized_url
        robots_sitemaps = _robots_sitemaps_for(
            f"{urlparse(normalized_url).scheme}://{urlparse(normalized_url).netloc}"
        )
        # Hard per-URL deadline over the WHOLE strategy chain (#adversarial 2.2):
        # a poisoned/tarpit URL must not hold its global concurrency slot for
        # (strategies x retries x timeout + browser challenge waits) ~ minutes
        # while the resource gate reports idle capacity. If the chain blows the
        # budget we fail the URL as network_error and free the slot.
        budget = float(
            (policy.get("limits") or {}).get("total_url_budget_seconds")
            or DEFAULT_TOTAL_URL_BUDGET_SECONDS
        )
        try:
            result = await asyncio.wait_for(
                _execute_crawl_chain(
                    original_url=original_url,
                    normalized_url=normalized_url,
                    policy=policy,
                    http_fetcher=fetcher,
                    cffi_fetcher=cffi,
                    browser_worker=browser_worker,
                    cache=cache or _SHARED_HTTP_CACHE,
                    # Content cache is opt-in (#5): only use the shared instance when explicitly
                    # enabled via settings, so test suites that fake per-test content for the
                    # same URL stay isolated. Callers may still pass an explicit ``content_cache``.
                    content_cache=content_cache
                    or (_get_shared_content_cache() if active_settings.content_cache_enabled else None),
                    scheduler=scheduler,
                    domain=job.domain,
                    robots_sitemaps=robots_sitemaps,
                    caller_validators=_validators_for(job),
                ),
                timeout=budget,
            )
        except TimeoutError:
            result = _failed_engine_result(
                original_url=original_url,
                normalized_url=normalized_url,
                strategy="",
                classification="network_error",
                error="url_budget_exceeded",
                diagnostics={"url_budget_seconds": budget, "error": "url_budget_exceeded"},
            )
        return index, result

    # --- robots.txt parsing + filtering helpers (#12) ---
    # robots_cache is module-level (shared across requests, #9); we reference it
    # directly from the nested helpers below.
    robots_ttl = 600.0
    # Negative-result TTL for robots fetch failures (M7): a blip must not
    # allow-list an origin for the full robots_ttl, nor block it for an hour.
    ROBOTS_FAILURE_TTL_SECONDS = 60.0

    def _purge_robots_cache(now: float | None = None) -> int:
        """Remove expired entries from robots cache; return number removed."""
        if now is None:
            now = time.monotonic()
        with _ROBOTS_CACHE_LOCK:
            expired = [origin for origin, (expires, _, _) in robots_cache.items() if expires <= now]
            for origin in expired:
                del robots_cache[origin]
        return len(expired)

    def _parse_robots_rules(text: str) -> list[tuple[bool, str]]:
        """RFC 9309 group selection: parse discrete user-agent groups and apply
        only the most-specific group matching our product token (falling back to
        the ``*`` group). Previously rules were unioned across ALL groups, so a
        Baiduspider ``Disallow: /`` blocked our crawler site-wide."""
        our_token = USER_AGENT_TOKEN
        groups: list[tuple[list[str], list[tuple[bool, str]]]] = []
        last_agents: list[str] = []
        current: tuple[list[str], list[tuple[bool, str]]] | None = None
        for raw in text.lstrip("\ufeff").splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            key, _, value = line.partition(":")
            key = key.strip().lower()
            value = value.strip()
            if key == "user-agent":
                if current is not None:
                    groups.append(current)
                    current = None
                last_agents.append(value.lower())
            elif key in ("disallow", "allow") and value is not None:
                if current is None:
                    current = (list(last_agents), [])
                    last_agents = []
                current[1].append((key == "allow", value))
        if current is not None:
            groups.append(current)

        best_rules: list[tuple[bool, str]] | None = None
        best_len = -1
        star_rules: list[tuple[bool, str]] | None = None
        for agents, grules in groups:
            for agent in agents:
                if agent == "*":
                    if star_rules is None:
                        star_rules = grules
                    break
                # Match the product token, not the full User-Agent string:
                # URL/version fragments in our UA must not claim groups, and
                # this is the same token _parse_robots_crawl_delay uses (M7).
                if agent and agent in our_token and len(agent) > best_len:
                    best_len = len(agent)
                    best_rules = grules
                    break
        if best_rules is not None:
            return best_rules
        return star_rules if star_rules is not None else []

    def _parse_robots_sitemaps(text: str) -> list[str]:
        """Extract ``Sitemap:`` directive URLs from a robots.txt body (#4).

        These are the sitemap locations discovery diagnostics would otherwise
        re-fetch from /robots.txt; caching them here lets the engine pass them
        down so discovery skips its own robots fetch.
        """
        out: list[str] = []
        for raw in text.lstrip("\ufeff").splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            key, _, value = line.partition(":")
            if key.strip().lower() == "sitemap" and value.strip():
                out.append(value.strip())
        return out

    async def _fetch_robots_rules(origin: str) -> tuple[list[tuple[bool, str]], list[str]]:
        now = time.monotonic()
        # Periodically purge expired entries to prevent unbounded growth.
        # The len() read is intentionally outside the lock: it is a cheap
        # pre-check that avoids a lock acquisition on the hot path. The
        # purge + insert below acquire the lock so the actual mutation is
        # atomic; the worst-case race is one extra purge per origin, never
        # a corruption of the dict (adversarial review F-robots-cache-race).
        with _ROBOTS_CACHE_LOCK:
            cache_size = len(robots_cache)
        if cache_size > ROBOTS_CACHE_MAX_ENTRIES:
            _purge_robots_cache(now)
        with _ROBOTS_CACHE_LOCK:
            cached = robots_cache.get(origin)
            if cached and cached[0] > now:
                return cached[1], cached[2], cached[3]
        rules: list[tuple[bool, str]] = []
        sitemaps: list[str] = []
        crawl_delay: float | None = None
        robots_url = urljoin(origin, "/robots.txt")
        # Lock released before the blocking network call so concurrent
        # crawls for different origins do not serialize on the lock.
        body, status = await _fetch_and_decode(fetcher, robots_url, {}, policy["limits"]["timeout_seconds"])
        ttl = robots_ttl
        if status == 200 and body is not None:
            rules = _parse_robots_rules(body)
            sitemaps = _parse_robots_sitemaps(body)
            crawl_delay = _parse_robots_crawl_delay(body)
        elif status == 404:
            # No robots.txt: nothing to enforce.
            pass
        else:
            # 5xx, other statuses, or transport failure: fail closed by
            # disallowing everything (M7). Cached on a short TTL so a blip
            # cannot allow-list an origin, while a recovery is picked up
            # quickly and a prolonged outage stays blocked.
            rules = [(False, "/")]
            ttl = min(robots_ttl, ROBOTS_FAILURE_TTL_SECONDS)
        with _ROBOTS_CACHE_LOCK:
            robots_cache[origin] = (now + ttl, rules, sitemaps, crawl_delay)
            # Hard cap: evict oldest origins if still over the limit (e.g. a crawl
            # that discovers thousands of unique, still-fresh domains).
            _cap_robots_cache(robots_cache, ROBOTS_CACHE_MAX_ENTRIES)
        return rules, sitemaps, crawl_delay

    async def _robots_allows_target(target_url: str) -> bool:
        parsed = urlparse(target_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        rules, _sitemaps, _crawl_delay = await _fetch_robots_rules(origin)
        return _robots_allows(target_url, rules)

    async def _robots_crawl_delay_for(target_url: str) -> float | None:
        """Return the Crawl-delay (seconds) declared by the target's origin, or
        None. Uses the shared robots cache so the value is fetched at most once
        per origin per TTL window (adversarial perf #2.6)."""
        parsed = urlparse(target_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        _rules, _sitemaps, crawl_delay = await _fetch_robots_rules(origin)
        return crawl_delay

    def _robots_sitemaps_for(origin: str) -> list[str] | None:
        """Return cached robots Sitemap URLs for an origin, or ``None`` if the
        engine never fetched robots.txt for it (e.g. respect_robots=False).

        Returning ``None`` (not ``[]``) matters: an empty list would tell
        discovery "robots was fetched, no sitemaps", preventing its own fetch,
        while ``None`` lets discovery fetch robots itself when needed (#4).
        """
        with _ROBOTS_CACHE_LOCK:
            cached = robots_cache.get(origin)
        if cached and cached[0] > time.monotonic():
            return list(cached[2])
        return None

    def _robots_disallowed_result(job: CrawlTask) -> EngineCrawlResult:
        return _skipped_engine_result(
            original_url=job.original_url,
            normalized_url=job.normalized_url,
            strategy="HTTP_DECLARED_CRAWLER",
            classification="robots_disallowed",
            error="robots_txt_disallowed",
            diagnostics={"robots": {"allowed": False}},
        )

    async def _sitemap_seed_urls() -> list[str]:
        # Fetch each seed origin's /sitemap.xml concurrently: slow/unreachable
        # origins must not serialize the others' seeding. Seeds are hostile
        # input: normalize every <loc> and apply the scope filter before it can
        # reach the frontier (adversarial review M-5).
        async def seeds_for(job: CrawlTask) -> list[str]:
            parsed = urlparse(job.normalized_url)
            origin = f"{parsed.scheme}://{parsed.netloc}"
            sitemap_url = urljoin(origin, "/sitemap.xml")
            # Recursive: follows sitemap indexes down to page URLs.
            found = await fetch_sitemap_urls(fetcher, sitemap_url, policy["limits"]["timeout_seconds"])
            normalized_seeds: list[str] = []
            for loc, lastmod in found:
                try:
                    normalized_seed = normalize_public_http_url(loc)
                except ValueError:
                    continue
                if scope_pattern is not None and not scope_pattern.search(normalized_seed):
                    continue
                # #3: skip URLs whose content we already cache and that the
                # sitemap says have not changed since we cached them.
                if _seed_is_fresh(normalized_seed, lastmod, content_cache, policy):
                    continue
                normalized_seeds.append(normalized_seed)
            return normalized_seeds

        per_job = await asyncio.gather(*[seeds_for(job) for job in jobs])
        return [seed for seeds in per_job for seed in seeds]

    # --- crawl orchestration ---
    limits = policy["limits"]
    max_depth = int(limits.get("max_depth") or 0)
    # F4: 0 is no longer "unlimited"; the policy always emits a bounded
    # value (DEFAULT_MAX_PAGES when unset), but guard here too so a 0 never
    # loops forever.
    max_pages = int(limits.get("max_pages") or 0)
    respect_robots = bool(limits.get("respect_robots"))
    include_html = bool((policy.get("browser_profile") or {}).get("include_html", True))
    discovery = policy.get("discovery") or {}
    scope_regex = policy.get("scope_regex")
    # Compile the user-supplied scope regex ONCE with the safe guard so the
    # recursive loop never applies a raw re.search (ReDoS) to every link.
    scope_pattern = _compile_scope_regex_safe(scope_regex)
    recursive = max_depth > 0
    # Document bodies (PDFs etc.) yield no further HTML links: unless the
    # caller explicitly wants file contents, expansion records their URLs but
    # never downloads them.
    want_document_bodies = policy.get("extraction", DEFAULT_EXTRACTION) == EXTRACTION_DOCUMENT

    async def _worker(job: CrawlTask) -> EngineCrawlResult:
        _, result = await run_job(job)
        return result

    # #2-stream: serialize each page result to disk as its batch completes so
    # peak memory scales with batch size, not total pages. RAM never holds the
    # full result set; the response is reassembled from the spill at the end.
    spill = _ResultSpill(spill_path)

    if not recursive:
        allowed_jobs: list[CrawlTask] = []
        for job in jobs:
            if respect_robots:
                if not await _robots_allows_target(job.normalized_url):
                    await _spill_write(spill, job.index, _robots_disallowed_result(job))
                    continue
                # Honor a host's robots Crawl-delay as a per-domain minimum
                # inter-request delay (adversarial perf #2.6). Applied on top of
                # the global/adaptive pacing inside the scheduler.
                crawl_delay = await _robots_crawl_delay_for(job.normalized_url)
                if crawl_delay and crawl_delay > 0:
                    scheduler.set_domain_min_delay(job.domain, crawl_delay)
            allowed_jobs.append(job)

        # #3.1: stream each completed result to disk immediately so peak RAM tracks
        # the in-flight set, not the whole batch. Heavy payloads are stripped
        # before spilling when the caller did not request them (#1).
        async def _spill_one(index: int, result: EngineCrawlResult) -> None:
            if not include_html:
                result = replace(result, html=None, text=None)
            await _spill_write(spill, index, result)

        if allowed_jobs:
            await scheduler.run(allowed_jobs, _worker, on_result=_spill_one)
    else:
        # Two-tier weighted admission: size HTTP concurrency to the live RAM/CPU
        # headroom (RAM-derived from the cgroup limit and browser-pool reserve)
        # so the cheap HTTP path scales wide while the browser tier stays
        # RAM-budgeted. Replaces the old stop-the-world pause.
        _admission = WeightedAdmission(
            _resource_gate,
            _build_admission_policy(
                read_memory_stats().total_bytes or 0,
                pool_size=derive_browser_pool_size(),
                max_concurrent_pages=active_settings.max_concurrent_pages,
            ),
        )
        await run_recursive_crawl(
            jobs=jobs,
            scheduler=scheduler,
            worker=_worker,
            robots_allows=_robots_allows_target,
            robots_crawl_delay=_robots_crawl_delay_for,
            robots_disallowed=_robots_disallowed_result,
            sitemap_seeds=_sitemap_seed_urls,
            spill=spill,
            shutdown_event=shutdown_event,
            max_depth=max_depth,
            max_pages=max_pages,
            respect_robots=respect_robots,
            include_html=include_html,
            want_document_bodies=want_document_bodies,
            scope_pattern=scope_pattern,
            global_concurrency=active_settings.global_concurrency,
            seed_sitemaps=bool(discovery.get("sitemaps")),
            resource_gate=_resource_gate,
            admission=_admission,
        )
    response = {
        "request_id": f"crawl_{uuid.uuid4().hex[:12]}",
        "submitted_count": len(urls),
        # Report the service's own schema version: echoing the caller-supplied
        # value would let responses claim a contract the service never ran.
        "policy_schema_version": POLICY_SCHEMA_VERSION,
        # #2-stream: reassembled from the on-disk spill in submission order.
        "results": spill.load_ordered(),
    }
    if webhook_url is not None:
        response["webhook_url"] = webhook_url
    spill.close()
    return response
