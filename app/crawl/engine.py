"""Crawl execution engine.

Owns the policy-driven crawl orchestration: payload/URL/policy parsing,
the per-URL strategy chain (HTTP -> browser fallback), worker dispatch, and
the final result assembly. This module is the single entry point that the
FastAPI layer (``app.main``) calls into.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import functools
from collections import deque
import time
import uuid
from urllib.parse import urljoin, urlparse
from dataclasses import dataclass, fields, replace
from typing import Any

from app.classify import classify_page_with_evidence
from app.api_models import POLICY_SCHEMA_VERSION
from app.constants import (
    BROWSER_HEADLESS,
    BROWSER_HEADED_WARMED,
    BROWSER_BLOCKED_CLASSIFICATIONS,
    HTTP_BLOCKED_CLASSIFICATIONS,
    DEFAULT_EXTRACTION,
    DEFAULT_STRATEGIES,
    EXTRACTION_DOCUMENT,
    EXTRACTION_FULL,
    EXTRACTION_METHOD_BINARY_NO_TEXT,
    EXTRACTION_METHOD_DOCUMENT,
    EXTRACTION_METHOD_DOM_TEXT,
    EXTRACTION_METHOD_HTTP_BODY,
    EXTRACTION_METHOD_HTTP_HTML,
    EXTRACTION_METHOD_TRAFILATURA,
    EXTRACTION_TRAFILATURA,
    HTTP_CFFI_IMPERSONATE,
    HTTP_DECLARED_CRAWLER,
    SUPPORTED_EXTRACTIONS,
    SUPPORTED_STRATEGIES,
    HTTP_STRATEGIES,
    BROWSER_STRATEGIES,
)
from app.crawl.cache import HttpCache
from app.crawl.content_cache import ContentCache
from app.crawl.extraction import (
    _detect_pagination,
    _extract_discovered_links,
    _parse_html_tree,
    _extract_document,
    _extract_html,
    extract_js_endpoints,
    _html_diagnostics,
    extract_content,
)
from app.crawl.fetch_helpers import _fetch_and_decode, fetch_sitemap_urls
from app.crawl.payload_helpers import _payload_policy, _payload_urls, SUPPORTED_EXTRACTION_METHODS
from app.crawl.result_helpers import (
    _binary_no_text_result,
    _blocked_engine_result,
    _failed_engine_result,
    _skipped_engine_result,
)
from app.crawl.strategies.browser import BrowserRenderer
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
    RenderOptions,
    EngineCrawlResult,
    CrawlWorker,
    HttpFetcher,
    to_dict,
)
from app.scheduler import DomainScheduler
from app.settings import CrawlerSettings
from app.validation import normalize_public_http_url, registrable_domain


@dataclass(frozen=True)
class CrawlTask:
    index: int
    original_url: str
    normalized_url: str
    domain: str



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


def _filter_links_by_scope(result: EngineCrawlResult, scope_regex: str | None) -> EngineCrawlResult:
    pattern = _compile_scope_regex_safe(scope_regex)
    if pattern is None:
        return result
    # Bound the number of links scanned so a huge link set cannot amplify cost.
    links = result.discovered_links[:_MAX_SCOPE_LINKS]
    filtered = [link for link in links if pattern.search(link.normalized_url)]
    # #17: use dataclasses.replace (shares field references) instead of asdict,
    # which deep-copies every field (html/text/markdown + the full link list)
    # on every successful result in a crawl.
    return replace(
        result,
        discovered_links=filtered,
        discovered_link_count=len(filtered),
        discovered_links_truncated=result.discovered_links_truncated
        and len(filtered) < result.discovered_link_count,
    )


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
            extracted = _extract_document(
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
            extracted = extract_content(content, None, result.final_url, css_selector=policy.get("css_selector"), xpath=policy.get("xpath"))
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
    for strategy in policy["strategies"]:
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
                    scoped = _filter_links_by_scope(retry_result, policy.get("scope_regex"))
                    return EngineCrawlResult(**{f.name: getattr(scoped, f.name) for f in fields(EngineCrawlResult) if f.name != "attempted_strategies"}, attempted_strategies=attempts)
                last_result = result

        if result.status == "success" and (result.quality_score > 0 or result.classification == "not_modified"):
            scoped = _filter_links_by_scope(result, policy.get("scope_regex"))
            return EngineCrawlResult(**{field.name: getattr(scoped, field.name) for field in fields(EngineCrawlResult) if field.name != "attempted_strategies"}, attempted_strategies=attempts)
    assert last_result is not None
    scoped = _filter_links_by_scope(last_result, policy.get("scope_regex"))
    return EngineCrawlResult(**{field.name: getattr(scoped, field.name) for field in fields(EngineCrawlResult) if field.name != "attempted_strategies"}, attempted_strategies=attempts)


@functools.lru_cache(maxsize=1024)
def _robots_pattern_to_regex(pattern: str) -> "re.Pattern[str]":
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
robots_cache: "dict[str, tuple[float, list[tuple[bool, str]], list[str]]]" = {}


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
) -> dict[str, Any]:
    active_settings = settings or CrawlerSettings.from_mapping()
    urls = _payload_urls(payload)
    if len(urls) > active_settings.max_batch_size:
        raise ValueError("batch_size_exceeded")
    policy = _payload_policy(payload, active_settings)
    fetcher = http_fetcher or _default_http_fetcher
    cffi = cffi_fetcher or _default_cffi_fetcher
    shutdown_event = shutdown_event or asyncio.Event()

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
        global_limit=active_settings.global_concurrency,
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
        result = await _execute_crawl_chain(
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
        )
        return index, result

    # --- robots.txt parsing + filtering helpers (#12) ---
    # robots_cache is module-level (shared across requests, #9); we reference it
    # directly from the nested helpers below.
    robots_ttl = 600.0

    def _purge_robots_cache(now: float | None = None) -> int:
        """Remove expired entries from robots cache; return number removed."""
        if now is None:
            now = time.monotonic()
        expired = [origin for origin, (expires, _, _) in robots_cache.items() if expires <= now]
        for origin in expired:
            del robots_cache[origin]
        return len(expired)

    def _parse_robots_rules(text: str) -> list[tuple[bool, str]]:
        """RFC 9309 group selection: parse discrete user-agent groups and apply
        only the most-specific group matching our product token (falling back to
        the ``*`` group). Previously rules were unioned across ALL groups, so a
        Baiduspider ``Disallow: /`` blocked our crawler site-wide."""
        our_token = "dev-stack-browser-crawler"
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

        ua_full = "dev-stack-browser-crawler/0.2 +https://local.dev-stack/crawler"
        best_rules: list[tuple[bool, str]] | None = None
        best_len = -1
        star_rules: list[tuple[bool, str]] | None = None
        for agents, grules in groups:
            for agent in agents:
                if agent == "*":
                    if star_rules is None:
                        star_rules = grules
                    break
                if agent and agent in ua_full and len(agent) > best_len:
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
        # Periodically purge expired entries to prevent unbounded growth
        if len(robots_cache) > ROBOTS_CACHE_MAX_ENTRIES:
            _purge_robots_cache(now)
        cached = robots_cache.get(origin)
        if cached and cached[0] > now:
            return cached[1], cached[2]
        rules: list[tuple[bool, str]] = []
        sitemaps: list[str] = []
        robots_url = urljoin(origin, "/robots.txt")
        body = await _fetch_and_decode(fetcher, robots_url, {}, policy["limits"]["timeout_seconds"])
        if body is not None:
            rules = _parse_robots_rules(body)
            sitemaps = _parse_robots_sitemaps(body)
        robots_cache[origin] = (now + robots_ttl, rules, sitemaps)
        # Hard cap: evict oldest origins if still over the limit (e.g. a crawl
        # that discovers thousands of unique, still-fresh domains).
        _cap_robots_cache(robots_cache, ROBOTS_CACHE_MAX_ENTRIES)
        return rules, sitemaps

    async def _robots_allows_target(target_url: str) -> bool:
        parsed = urlparse(target_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        rules, _sitemaps = await _fetch_robots_rules(origin)
        return _robots_allows(target_url, rules)

    def _robots_sitemaps_for(origin: str) -> list[str] | None:
        """Return cached robots Sitemap URLs for an origin, or ``None`` if the
        engine never fetched robots.txt for it (e.g. respect_robots=False).

        Returning ``None`` (not ``[]``) matters: an empty list would tell
        discovery "robots was fetched, no sitemaps", preventing its own fetch,
        while ``None`` lets discovery fetch robots itself when needed (#4).
        """
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
            for loc, _lastmod in found:
                try:
                    normalized_seed = normalize_public_http_url(loc)
                except ValueError:
                    continue
                if scope_pattern is not None and not scope_pattern.search(normalized_seed):
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

    async def _worker(job: CrawlTask) -> EngineCrawlResult:
        _, result = await run_job(job)
        return result

    if not recursive:
        by_index: dict[int, EngineCrawlResult] = {}
        allowed_jobs: list[CrawlTask] = []
        for job in jobs:
            if respect_robots and not await _robots_allows_target(job.normalized_url):
                by_index[job.index] = _robots_disallowed_result(job)
            else:
                allowed_jobs.append(job)

        scheduled_results = await scheduler.run(allowed_jobs, _worker) if allowed_jobs else []
        for job, result in zip(allowed_jobs, scheduled_results):
            # Strip heavy payloads inside the loop so they are not retained for
            # the whole crawl when the caller did not ask for them (#1).
            if not include_html:
                result = replace(result, html=None, text=None)
            by_index[job.index] = result
    else:
        by_index: dict[int, EngineCrawlResult] = {}
        visited: set[str] = set()
        frontier: "deque[tuple[int, CrawlTask]]" = deque([(0, job) for job in jobs])
        next_index = len(jobs)
        page_count = 0
        # bounded recursive BFS (#11) seeded by sitemaps (#13)
        if discovery.get("sitemaps"):
            for seed in await _sitemap_seed_urls():
                if seed in visited:
                    continue
                frontier.append(
                    (0, CrawlTask(index=next_index, original_url=seed, normalized_url=seed, domain=registrable_domain(seed)))
                )
                next_index += 1
        while frontier and page_count < max_pages:
            if shutdown_event.is_set():
                break
            # Pull a batch sized to the configured concurrency (and the remaining
            # page budget) so discovered links are fetched *through the scheduler*
            # concurrently instead of strictly one-by-one. Without this the
            # recursive crawl ignores global_concurrency entirely.
            batch_size = min(
                len(frontier),
                max(1, active_settings.global_concurrency * 2),
                max(1, max_pages - page_count),
            )
            batch = [frontier.popleft() for _ in range(batch_size)]

            allowed_jobs: list[tuple[int, CrawlTask]] = []
            for depth, job in batch:
                if job.normalized_url in visited:
                    continue
                if respect_robots and not await _robots_allows_target(job.normalized_url):
                    by_index[job.index] = _robots_disallowed_result(job)
                    visited.add(job.normalized_url)
                    page_count += 1
                    continue
                allowed_jobs.append((depth, job))
                visited.add(job.normalized_url)

            scheduled_results = (
                await scheduler.run([job for _, job in allowed_jobs], _worker)
                if allowed_jobs
                else []
            )

            for (depth, job), result in zip(allowed_jobs, scheduled_results):
                # Strip heavy payloads inside the loop so they are not retained
                # for the whole crawl when the caller did not ask for them (#1).
                if not include_html:
                    result = replace(result, html=None, text=None)
                by_index[job.index] = result
                page_count += 1
                if depth >= max_depth:
                    continue
                for link in result.discovered_links:
                    if link.normalized_url in visited:
                        continue
                    if scope_pattern is not None and not scope_pattern.search(link.normalized_url):
                        continue
                    if respect_robots and not await _robots_allows_target(link.normalized_url):
                        continue
                    if max_pages and page_count + len(frontier) >= max_pages:
                        break
                    frontier.append(
                        (
                            depth + 1,
                            CrawlTask(index=next_index, original_url=link.url, normalized_url=link.normalized_url, domain=link.host),
                        )
                    )
                    next_index += 1

    ordered_results = [by_index[index] for index in sorted(by_index)]
    response = {
        "request_id": f"crawl_{uuid.uuid4().hex[:12]}",
        "submitted_count": len(urls),
        # Report the service's own schema version: echoing the caller-supplied
        # value would let responses claim a contract the service never ran.
        "policy_schema_version": POLICY_SCHEMA_VERSION,
        "results": [to_dict(result) for result in ordered_results],
    }
    if webhook_url is not None:
        response["webhook_url"] = webhook_url
    return response
