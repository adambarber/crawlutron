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
import time
import uuid
from urllib.parse import urljoin, urlparse
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, fields, replace
from typing import Any

from app.classify import classify_rendered_page_with_evidence
from app.constants import (
    BROWSER_HEADLESS,
    BROWSER_HEADED_WARMED,
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
)
from app.crawl.cache import HttpCache
from app.crawl.extraction import (
    _detect_pagination,
    _extract_discovered_links,
    _extract_document,
    _extract_html,
    extract_js_endpoints,
    _html_diagnostics,
    extract_content,
)
from app.crawl.strategies.browser import BrowserRenderer
from app.crawl.strategies.http import (
    _default_cffi_fetcher,
    _default_http_fetcher,
    _http_strategy_result,
    _quality_score,
)
from app.models import CrawlJob, CrawlResult, CrawlStrategyAttempt, RenderOptions, UniversalCrawlResult
from app.scheduler import DomainScheduler
from app.settings import CrawlerSettings
from app.validation import normalize_public_http_url, registrable_domain


CrawlWorker = Callable[[CrawlJob], Awaitable[CrawlResult] | CrawlResult]
HttpFetcher = Callable[[str, dict[str, str], int], Any]


@dataclass(frozen=True)
class CrawlExecutionJob:
    index: int
    original_url: str
    normalized_url: str
    domain: str


SUCCESS_CLASSIFICATIONS = {"rendered"}
BLOCKED_CLASSIFICATIONS = {
    "captcha_detected",
    "bot_challenge_detected",
    "access_denied",
    "login_required",
    "paywall_detected",
    "rate_limited",
    "cookie_consent_required",
    "javascript_required",
    "unsupported_browser",
    "geolocation_blocked",
    "waf_challenge_detected",
    "not_modified",
}
HTTP_STRATEGIES = {HTTP_DECLARED_CRAWLER, HTTP_CFFI_IMPERSONATE}
BROWSER_STRATEGIES = {BROWSER_HEADLESS, BROWSER_HEADED_WARMED}
SUPPORTED_CRAWL_STRATEGIES = HTTP_STRATEGIES | BROWSER_STRATEGIES
DEFAULT_CRAWL_STRATEGIES = list(DEFAULT_STRATEGIES)
SUPPORTED_EXTRACTION_METHODS = set(SUPPORTED_EXTRACTIONS)
DEFAULT_EXTRACTION = DEFAULT_EXTRACTION

# Shared conditional cache so repeated crawls can leverage 304 responses.
_SHARED_HTTP_CACHE = HttpCache()


def _payload_urls(payload: dict[str, Any]) -> list[str]:
    if "urls" in payload:
        urls = payload["urls"]
        if not isinstance(urls, list) or not all(isinstance(url, str) for url in urls):
            raise ValueError("invalid_urls")
        return urls
    if "url" in payload and isinstance(payload["url"], str):
        return [payload["url"]]
    raise ValueError("missing_url")


def _payload_policy(payload: dict[str, Any], settings: CrawlerSettings) -> dict[str, Any]:
    from app.api_models import POLICY_SCHEMA_VERSION

    policy = payload.get("policy") or {}
    if not isinstance(policy, dict):
        raise ValueError("invalid_policy")
    limits = policy.get("limits") or {}
    if not isinstance(limits, dict):
        raise ValueError("invalid_policy_limits")
    headers = policy.get("headers") or {}
    if not isinstance(headers, dict):
        raise ValueError("invalid_policy_headers")
    strategies = policy.get("strategies") or DEFAULT_CRAWL_STRATEGIES
    if not isinstance(strategies, list) or not all(isinstance(strategy, str) for strategy in strategies) or not strategies:
        raise ValueError("invalid_policy_strategies")
    if any(strategy not in SUPPORTED_CRAWL_STRATEGIES for strategy in strategies):
        raise ValueError("invalid_policy_strategy")
    extraction = policy.get("extraction") or DEFAULT_EXTRACTION
    if extraction not in SUPPORTED_EXTRACTION_METHODS:
        raise ValueError("invalid_policy_extraction")
    request_profile = policy.get("request_profile") or {}
    browser_profile = policy.get("browser_profile") or {}
    discovery = policy.get("discovery") or {}
    if not isinstance(request_profile, dict) or not isinstance(browser_profile, dict) or not isinstance(discovery, dict):
        raise ValueError("invalid_policy_profile")
    try:
        max_depth = max(0, min(20, int(limits.get("max_depth", 0))))
        max_pages = max(0, min(2000, int(limits.get("max_pages", 0))))
        respect_robots = bool(limits.get("respect_robots", True))
    except (TypeError, ValueError):
        raise ValueError("invalid_policy_limits") from None
    return {
        "schema_version": str(policy.get("schema_version", POLICY_SCHEMA_VERSION)),
        "strategies": strategies,
        "extraction": extraction,
        "scope_regex": policy.get("scope_regex"),
        "css_selector": policy.get("css_selector"),
        "xpath": policy.get("xpath"),
        "limits": {
            "timeout_seconds": int(limits.get("timeout_seconds", settings.default_timeout_seconds)),
            "max_retries": int(limits.get("max_retries", settings.default_max_retries)),
            "challenge_wait_seconds": int(limits.get("challenge_wait_seconds", 15)),
            "max_discovered_links": int(limits.get("max_discovered_links", 250)),
            "respect_robots": respect_robots,
            "max_depth": max_depth,
            "max_pages": max_pages,
        },
        "headers": {str(key): str(value) for key, value in headers.items()},
        "request_profile": dict(request_profile),
        "browser_profile": dict(browser_profile),
        "discovery": {str(key): bool(value) for key, value in discovery.items()},
    }


async def _call_worker(worker: CrawlWorker, job: CrawlJob) -> CrawlResult:
    result = worker(job)
    if inspect.iscoroutine(result):
        return await result
    return await asyncio.to_thread(worker, job)


def _result_dict(result: CrawlResult) -> dict[str, Any]:
    return asdict(result)


def _universal_result_dict(result: UniversalCrawlResult) -> dict[str, Any]:
    return asdict(result)


def _filter_links_by_scope(result: UniversalCrawlResult, scope_regex: str | None) -> UniversalCrawlResult:
    if not scope_regex:
        return result
    try:
        pattern = re.compile(scope_regex)
    except re.error:
        return result
    filtered = [link for link in result.discovered_links if pattern.search(link.normalized_url)]
    return UniversalCrawlResult(
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
) -> UniversalCrawlResult:
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
    return UniversalCrawlResult(
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


async def _execute_crawl_chain(
    *,
    original_url: str,
    normalized_url: str,
    policy: dict[str, Any],
    http_fetcher: HttpFetcher,
    cffi_fetcher: HttpFetcher,
    browser_worker: CrawlWorker | None,
    cache: HttpCache | None = None,
    scheduler: DomainScheduler | None = None,
    domain: str = "",
) -> UniversalCrawlResult:
    attempts: list[CrawlStrategyAttempt] = []
    last_result: UniversalCrawlResult | None = None
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
            )
        elif strategy in BROWSER_STRATEGIES:
            if browser_worker is None:
                result = UniversalCrawlResult(
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
            result = UniversalCrawlResult(
                original_url=original_url,
                normalized_url=normalized_url,
                final_url=None,
                status="failed",
                classification="unknown_error",
                selected_strategy=None,
                attempted_strategies=[
                    CrawlStrategyAttempt(
                        strategy=strategy,  # type: ignore[arg-type]
                        status="failed",
                        classification="unknown_error",
                        http_status=None,
                        content_type=None,
                        elapsed_ms=0,
                        error="unsupported_strategy",
                    )
                ],
                http_status=None,
                headers={},
                content_type=None,
                html=None,
                text=None,
                extraction_method=None,
                quality_score=0.0,
                error="unsupported_strategy",
            )
        attempts.extend(result.attempted_strategies)
        last_result = result
        if result.classification == "rate_limited" and scheduler is not None:
            cooldown = result.diagnostics.get("cooldown") or {}
            retry_after = cooldown.get("retry_after_seconds")
            if retry_after is not None:
                scheduler.set_domain_cooldown(domain, float(retry_after))
        if result.status == "success" and result.quality_score > 0:
            scoped = _filter_links_by_scope(result, policy.get("scope_regex"))
            return UniversalCrawlResult(**{field.name: getattr(scoped, field.name) for field in fields(UniversalCrawlResult) if field.name != "attempted_strategies"}, attempted_strategies=attempts)
    assert last_result is not None
    scoped = _filter_links_by_scope(last_result, policy.get("scope_regex"))
    return UniversalCrawlResult(**{field.name: getattr(scoped, field.name) for field in fields(UniversalCrawlResult) if field.name != "attempted_strategies"}, attempted_strategies=attempts)


async def crawl_request(
    payload: dict[str, Any],
    *,
    settings: CrawlerSettings | None = None,
    browser_worker: CrawlWorker | None = None,
    http_fetcher: HttpFetcher | None = None,
    cffi_fetcher: HttpFetcher | None = None,
    cache: HttpCache | None = None,
    webhook_url: str | None = None,
) -> dict[str, Any]:
    active_settings = settings or CrawlerSettings.from_mapping()
    urls = _payload_urls(payload)
    if len(urls) > active_settings.max_batch_size:
        raise ValueError("batch_size_exceeded")
    policy = _payload_policy(payload, active_settings)
    fetcher = http_fetcher or _default_http_fetcher
    cffi = cffi_fetcher or _default_cffi_fetcher

    jobs: list[CrawlExecutionJob] = []
    for index, original_url in enumerate(urls):
        try:
            normalized_url = normalize_public_http_url(original_url)
            domain = registrable_domain(normalized_url)
        except ValueError as error:
            raise ValueError(f"invalid_url: {error}") from error
        jobs.append(CrawlExecutionJob(index=index, original_url=original_url, normalized_url=normalized_url, domain=domain))

    scheduler = DomainScheduler(
        global_limit=active_settings.global_concurrency,
        per_domain_limit=active_settings.per_domain_concurrency,
        delay_seconds=active_settings.per_domain_delay_ms / 1000,
    )

    async def run_job(job: CrawlExecutionJob) -> tuple[int, UniversalCrawlResult]:
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
            scheduler=scheduler,
            domain=job.domain,
        )
        return index, result

    # --- robots.txt parsing + filtering helpers (#12) ---
    robots_cache: dict[str, tuple[float, list[tuple[bool, str]]]] = {}
    robots_ttl = 600.0

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
        cached = robots_cache.get(origin)
        if cached and cached[0] > now:
            return cached[1]
        rules: list[tuple[bool, str]] = []
        robots_url = urljoin(origin, "/robots.txt")
        try:
            resp = fetcher(robots_url, {}, policy["limits"]["timeout_seconds"])
            if inspect.iscoroutine(resp):
                resp = await resp
            if resp.status_code == 200:
                from app.crawl.strategies.http import _decode_body, _decompress_body

                body = _decode_body(
                    _decompress_body(resp.body, resp.headers.get("content-encoding", "")),
                    resp.headers.get("content-type"),
                )
                rules = _parse_robots_rules(body)
        except Exception:  # noqa: BLE001
            rules = []
        robots_cache[origin] = (now + robots_ttl, rules)
        return rules

    def _robots_allows(target_url: str, rules: list[tuple[bool, str]]) -> bool:
        parsed = urlparse(target_url)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        allowed = True
        longest = -1
        for is_allow, pattern in rules:
            if not pattern:
                # "Disallow: " (empty) permits everything.
                if not is_allow and longest < 0:
                    allowed = True
                continue
            if path.startswith(pattern) and len(pattern) > longest:
                longest = len(pattern)
                allowed = is_allow
        return allowed

    async def _robots_allows_target(target_url: str) -> bool:
        parsed = urlparse(target_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        return _robots_allows(target_url, await _fetch_robots_rules(origin))

    def _robots_disallowed_result(job: CrawlExecutionJob) -> UniversalCrawlResult:
        attempt = CrawlStrategyAttempt(
            strategy="HTTP_DECLARED_CRAWLER",  # type: ignore[arg-type]
            status="skipped",
            classification="robots_disallowed",
            http_status=None,
            content_type=None,
            elapsed_ms=0,
            error="robots_txt_disallowed",
        )
        return UniversalCrawlResult(
            original_url=job.original_url,
            normalized_url=job.normalized_url,
            final_url=None,
            status="skipped",
            classification="robots_disallowed",
            selected_strategy=None,
            attempted_strategies=[attempt],
            http_status=None,
            headers={},
            content_type=None,
            html=None,
            text=None,
            markdown=None,
            extraction_method=None,
            quality_score=0.0,
            classification_evidence=[],
            diagnostics={"robots": {"allowed": False}},
            discovered_link_count=0,
            discovered_links_truncated=False,
            discovered_links=[],
            error="robots_txt_disallowed",
        )

    async def _sitemap_seed_urls() -> list[str]:
        seeds: list[str] = []
        for job in jobs:
            parsed = urlparse(job.normalized_url)
            origin = f"{parsed.scheme}://{parsed.netloc}"
            sitemap_url = urljoin(origin, "/sitemap.xml")
            try:
                resp = fetcher(sitemap_url, {}, policy["limits"]["timeout_seconds"])
                if inspect.iscoroutine(resp):
                    resp = await resp
                if resp.status_code == 200:
                    from app.crawl.strategies.http import _decode_body, _decompress_body

                    text = _decode_body(
                        _decompress_body(resp.body, resp.headers.get("content-encoding", "")),
                        resp.headers.get("content-type"),
                    )
                    seeds.extend(re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", text, flags=re.IGNORECASE))
            except Exception:  # noqa: BLE001
                continue
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
        by_index: dict[int, UniversalCrawlResult] = {}
        allowed_jobs: list[CrawlExecutionJob] = []
        for job in jobs:
            if respect_robots and not await _robots_allows_target(job.normalized_url):
                by_index[job.index] = _robots_disallowed_result(job)
            else:
                allowed_jobs.append(job)
        scheduled_pairs = await scheduler.run(allowed_jobs, run_job) if allowed_jobs else []
        for index, result in scheduled_pairs:
            by_index[index] = result
    else:
        by_index: dict[int, UniversalCrawlResult] = {}
        visited: set[str] = set()
        frontier: list[tuple[int, CrawlExecutionJob]] = [(0, job) for job in jobs]
        next_index = len(jobs)
        page_count = 0
        # bounded recursive BFS (#11) seeded by sitemaps (#13)
        if discovery.get("sitemaps"):
            for seed in await _sitemap_seed_urls():
                if seed in visited:
                    continue
                frontier.append(
                    (0, CrawlExecutionJob(index=next_index, original_url=seed, normalized_url=seed, domain=registrable_domain(seed)))
                )
                next_index += 1
        while frontier and (max_pages == 0 or page_count < max_pages):
            depth, job = frontier.pop(0)
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
                        CrawlExecutionJob(index=next_index, original_url=link.url, normalized_url=link.normalized_url, domain=link.host),
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
        "results": [_universal_result_dict(result) for result in ordered_results],
    }
    if webhook_url is not None:
        response["webhook_url"] = webhook_url
    return response
