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
from collections import deque
import time
import uuid
from urllib.parse import urljoin, urlparse
from dataclasses import asdict, dataclass, fields, replace
from typing import Any

from app.classify import classify_page_with_evidence
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
    _extract_document,
    _extract_html,
    extract_js_endpoints,
    _html_diagnostics,
    extract_content,
)
from app.crawl.fetch_helpers import _fetch_and_decode
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
        return re.compile(scope_regex)
    except re.error:
        return None


def _filter_links_by_scope(result: EngineCrawlResult, scope_regex: str | None) -> EngineCrawlResult:
    pattern = _compile_scope_regex_safe(scope_regex)
    if pattern is None:
        return result
    # Bound the number of links scanned so a huge link set cannot amplify cost.
    links = result.discovered_links[:_MAX_SCOPE_LINKS]
    filtered = [link for link in links if pattern.search(link.normalized_url)]
    return EngineCrawlResult(
        **{
            **asdict(result),
            "discovered_links": filtered,
            "discovered_link_count": len(filtered),
            "discovered_links_truncated": result.discovered_links_truncated
            and len(filtered) < result.discovered_link_count,
        }
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
    diagnostics: dict[str, Any] = {
        "browser_profile": policy.get("browser_profile") or {},
        "runtime": {"elapsed_ms": result.elapsed_ms, "final_url": result.final_url},
        "content_metrics": {"visible_text_length": len((text or "").strip()), "html_length": len((result.html or "").strip())},
        **_html_diagnostics(result.html, result.final_url),
    }
    intercepted = result.metadata.get("intercepted_xhr")
    if intercepted:
        diagnostics["intercepted_xhr"] = intercepted
    screenshot = result.metadata.get("screenshot")
    if screenshot:
        diagnostics["screenshot"] = screenshot

    quality = _quality_score(result.status, result.classification, text, result.html, None)
    discovered_links, discovered_link_count, discovered_links_truncated = (
        _extract_discovered_links(result.html, result.final_url, max_links=policy["limits"]["max_discovered_links"]) if result.status == "success" else ([], 0, False)
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
) -> EngineCrawlResult:
    attempts: list[CrawlStrategyAttempt] = []
    last_result: EngineCrawlResult | None = None
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
            )
        elif strategy in BROWSER_STRATEGIES:
            if browser_worker is None:
                result = EngineCrawlResult(
                    original_url=original_url,
                    normalized_url=normalized_url,
                    final_url=None,
                    status="failed",
                    classification="browser_crash",
                    selected_strategy=None,
                    attempted_strategies=[
                        CrawlStrategyAttempt(
                            strategy=strategy,  # type: ignore[arg-type]
                            status="failed",
                            classification="browser_crash",
                            http_status=None,
                            content_type=None,
                            elapsed_ms=0,
                            error="browser_worker_unavailable",
                        )
                    ],
                    http_status=None,
                    headers={},
                    content_type=None,
                    html=None,
                    text=None,
                    extraction_method=None,
                    quality_score=0.0,
                    diagnostics={"browser_profile": policy.get("browser_profile") or {}, "runtime": {"error": "browser_worker_unavailable"}},
                    error="browser_worker_unavailable",
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
        if result.status == "success" and result.quality_score > 0:
            scoped = _filter_links_by_scope(result, policy.get("scope_regex"))
            return EngineCrawlResult(**{field.name: getattr(scoped, field.name) for field in fields(EngineCrawlResult) if field.name != "attempted_strategies"}, attempted_strategies=attempts)
    assert last_result is not None
    scoped = _filter_links_by_scope(last_result, policy.get("scope_regex"))
    return EngineCrawlResult(**{field.name: getattr(scoped, field.name) for field in fields(EngineCrawlResult) if field.name != "attempted_strategies"}, attempted_strategies=attempts)


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

    async def run_job(job: CrawlTask) -> tuple[int, EngineCrawlResult]:
        index = job.index
        original_url = job.original_url
        normalized_url = job.normalized_url
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
        )
        return index, result

    # --- robots.txt parsing + filtering helpers (#12) ---
    robots_cache: dict[str, tuple[float, list[tuple[bool, str]]]] = {}
    robots_ttl = 600.0

    def _purge_robots_cache(now: float | None = None) -> int:
        """Remove expired entries from robots cache; return number removed."""
        if now is None:
            now = time.monotonic()
        expired = [origin for origin, (expires, _) in robots_cache.items() if expires <= now]
        for origin in expired:
            del robots_cache[origin]
        return len(expired)

    def _parse_robots_rules(text: str) -> list[tuple[bool, str]]:
        rules: list[tuple[bool, str]] = []
        active = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            key, _, value = line.partition(":")
            key = key.strip().lower()
            value = value.strip()
            if key == "user-agent":
                active = value.lower() in ("*", "dev-stack-browser-crawler")
            elif key == "disallow" and active:
                rules.append((False, value))
            elif key == "allow" and active:
                rules.append((True, value))
        return rules

    async def _fetch_robots_rules(origin: str) -> list[tuple[bool, str]]:
        now = time.monotonic()
        # Periodically purge expired entries to prevent unbounded growth
        if len(robots_cache) > 1000:
            _purge_robots_cache(now)
        cached = robots_cache.get(origin)
        if cached and cached[0] > now:
            return cached[1]
        rules: list[tuple[bool, str]] = []
        robots_url = urljoin(origin, "/robots.txt")
        body = await _fetch_and_decode(fetcher, robots_url, {}, policy["limits"]["timeout_seconds"])
        if body is not None:
            rules = _parse_robots_rules(body)
        robots_cache[origin] = (now + robots_ttl, rules)
        return rules

    async def _robots_allows_target(target_url: str) -> bool:
        parsed = urlparse(target_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        return _robots_allows(target_url, await _fetch_robots_rules(origin))

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
        seeds: list[str] = []
        for job in jobs:
            parsed = urlparse(job.normalized_url)
            origin = f"{parsed.scheme}://{parsed.netloc}"
            sitemap_url = urljoin(origin, "/sitemap.xml")
            text = await _fetch_and_decode(fetcher, sitemap_url, {}, policy["limits"]["timeout_seconds"])
            if text is not None:
                seeds.extend(re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", text, flags=re.IGNORECASE))
        return seeds

    # --- crawl orchestration ---
    limits = policy["limits"]
    max_depth = int(limits.get("max_depth") or 0)
    max_pages = int(limits.get("max_pages") or 0)
    respect_robots = bool(limits.get("respect_robots"))
    include_html = bool((policy.get("browser_profile") or {}).get("include_html", True))
    discovery = policy.get("discovery") or {}
    scope_regex = policy.get("scope_regex")
    recursive = max_depth > 0

    if not recursive:
        by_index: dict[int, EngineCrawlResult] = {}
        allowed_jobs: list[CrawlTask] = []
        for job in jobs:
            if respect_robots and not await _robots_allows_target(job.normalized_url):
                by_index[job.index] = _robots_disallowed_result(job)
            else:
                allowed_jobs.append(job)

        async def _worker(job: CrawlTask) -> EngineCrawlResult:
            _, result = await run_job(job)
            return result

        scheduled_results = await scheduler.run(allowed_jobs, _worker) if allowed_jobs else []
        for job, result in zip(allowed_jobs, scheduled_results):
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
        while frontier and (max_pages == 0 or page_count < max_pages):
            if shutdown_event.is_set():
                break
            depth, job = frontier.popleft()
            if job.normalized_url in visited:
                continue
            if respect_robots and not await _robots_allows_target(job.normalized_url):
                by_index[job.index] = _robots_disallowed_result(job)
                visited.add(job.normalized_url)
                page_count += 1
                continue
            visited.add(job.normalized_url)
            _, result = await run_job(job)
            by_index[job.index] = result
            page_count += 1
            if depth >= max_depth:
                continue
            for link in result.discovered_links:
                if link.normalized_url in visited:
                    continue
                if scope_regex and not re.search(scope_regex, link.normalized_url):
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

    # memory-efficient recursive crawls (#8): strip heavy payloads when not requested
    if not include_html:
        for index, res in by_index.items():
            by_index[index] = replace(res, html=None, text=None)

    ordered_results = [by_index[index] for index in sorted(by_index)]
    response = {
        "request_id": f"crawl_{uuid.uuid4().hex[:12]}",
        "submitted_count": len(urls),
        "policy_schema_version": policy["schema_version"],
        "results": [to_dict(result) for result in ordered_results],
    }
    if webhook_url is not None:
        response["webhook_url"] = webhook_url
    return response
