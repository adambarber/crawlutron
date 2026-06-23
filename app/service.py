from __future__ import annotations

import re
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from html import unescape
from typing import Any

from .api_models import POLICY_SCHEMA_VERSION
from .classify import classify_rendered_page
from .models import CrawlJob, CrawlResult, CrawlStrategyAttempt, HttpFetchResult, RenderOptions, UniversalCrawlResult
from .scheduler import DomainScheduler
from .settings import CrawlerSettings
from .validation import normalize_public_http_url, registrable_domain


CrawlWorker = Callable[[CrawlJob], Awaitable[CrawlResult] | CrawlResult]
HttpFetcher = Callable[[str, dict[str, str], int], HttpFetchResult]
DocumentTextExtractor = Callable[[bytes, str | None, str], str]
TrafilaturaExtractor = Callable[[str, str], str]


@dataclass(frozen=True)
class CrawlExecutionJob:
    index: int
    original_url: str
    normalized_url: str
    domain: str

SUCCESS_CLASSIFICATIONS = {"rendered"}
BLOCKED_CLASSIFICATIONS = {"captcha_detected", "bot_challenge_detected", "access_denied", "login_required", "paywall_detected", "rate_limited"}
HTTP_STRATEGIES = {"HTTP_DECLARED_CRAWLER", "HTTP_SITE_COMPAT", "DOCUMENT_EXTRACT", "TRAFILATURA_EXTRACT"}
BROWSER_STRATEGIES = {"BROWSER_HEADLESS", "BROWSER_HEADED_WARMED"}
SUPPORTED_CRAWL_STRATEGIES = HTTP_STRATEGIES | BROWSER_STRATEGIES
DEFAULT_CRAWL_STRATEGIES = ["HTTP_DECLARED_CRAWLER", "BROWSER_HEADLESS"]


def _payload_urls(payload: dict[str, Any]) -> list[str]:
    if "urls" in payload:
        urls = payload["urls"]
        if not isinstance(urls, list) or not all(isinstance(url, str) for url in urls):
            raise ValueError("invalid_urls")
        return urls
    if "url" in payload and isinstance(payload["url"], str):
        return [payload["url"]]
    raise ValueError("missing_url")


def _render_options(payload: dict[str, Any], settings: CrawlerSettings) -> RenderOptions:
    options = payload.get("options") or {}
    if not isinstance(options, dict):
        raise ValueError("invalid_options")
    return RenderOptions(
        timeout_seconds=int(options.get("timeout_seconds", settings.default_timeout_seconds)),
        wait_until=str(options.get("wait_until", "network_idle_or_timeout")),
        max_retries=int(options.get("max_retries", settings.default_max_retries)),
        respect_robots=bool(options.get("respect_robots", settings.respect_robots)),
        include_html=bool(options.get("include_html", True)),
        include_text=bool(options.get("include_text", True)),
    )


def _disallowed_result(index_url: tuple[int, str], classification: str, elapsed_ms: int = 0) -> CrawlResult:
    index, url = index_url
    return CrawlResult(
        url=url,
        status="disallowed",
        classification=classification,  # type: ignore[arg-type]
        final_url=None,
        html=None,
        text=None,
        attempts=0,
        elapsed_ms=elapsed_ms,
        domain="",
        error=classification,
    )


async def _call_worker(worker: CrawlWorker, job: CrawlJob) -> CrawlResult:
    result = worker(job)
    if hasattr(result, "__await__"):
        return await result  # type: ignore[misc]
    return result  # type: ignore[return-value]


def _result_dict(result: CrawlResult) -> dict[str, Any]:
    return asdict(result)


def _universal_result_dict(result: UniversalCrawlResult) -> dict[str, Any]:
    return asdict(result)


def _default_http_fetcher(url: str, headers: dict[str, str], timeout_seconds: int) -> HttpFetchResult:
    request = urllib.request.Request(url, headers=headers)
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return HttpFetchResult(
                url=url,
                final_url=response.geturl(),
                status_code=response.status,
                headers={key.lower(): value for key, value in response.headers.items()},
                body=response.read(),
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
    except urllib.error.HTTPError as error:
        return HttpFetchResult(
            url=url,
            final_url=error.geturl(),
            status_code=error.code,
            headers={key.lower(): value for key, value in error.headers.items()},
            body=error.read(),
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )


def _payload_policy(payload: dict[str, Any], settings: CrawlerSettings) -> dict[str, Any]:
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
    return {
        "schema_version": str(policy.get("schema_version", POLICY_SCHEMA_VERSION)),
        "strategies": strategies,
        "limits": {
            "timeout_seconds": int(limits.get("timeout_seconds", settings.default_timeout_seconds)),
            "max_retries": int(limits.get("max_retries", settings.default_max_retries)),
            "challenge_wait_seconds": int(limits.get("challenge_wait_seconds", 15)),
        },
        "headers": {str(key): str(value) for key, value in headers.items()},
    }


def _headers_for_strategy(strategy: str, policy_headers: dict[str, str]) -> dict[str, str]:
    headers = {"User-Agent": "dev-stack-browser-crawler/0.2 (+https://local.dev-stack/crawler)"}
    if strategy == "HTTP_SITE_COMPAT":
        headers.update(
            {
                "User-Agent": "Mozilla/5.0 (compatible; dev-stack-browser-crawler/0.2; +https://local.dev-stack/crawler)",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
    headers.update(policy_headers)
    return headers


def _decode_body(body: bytes, content_type: str | None) -> str:
    charset = "utf-8"
    if content_type:
        match = re.search(r"charset=([^;]+)", content_type, flags=re.IGNORECASE)
        if match:
            charset = match.group(1).strip()
    return body.decode(charset, errors="replace")


def _text_from_html(html: str) -> str:
    without_scripts = re.sub(r"<script\b[^>]*>.*?</script>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    without_styles = re.sub(r"<style\b[^>]*>.*?</style>", " ", without_scripts, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", without_styles)
    return re.sub(r"\s+", " ", unescape(text)).strip()


def _status_for_classification(classification: str) -> str:
    if classification in SUCCESS_CLASSIFICATIONS:
        return "success"
    if classification in BLOCKED_CLASSIFICATIONS:
        return "blocked"
    return "failed"


def _quality_score(status: str, classification: str, text: str | None, html: str | None, http_status: int | None) -> float:
    if status != "success" or classification != "rendered":
        return 0.0
    text_length = len((text or "").strip())
    html_length = len((html or "").strip())
    score = 0.45
    if http_status and 200 <= http_status < 300:
        score += 0.2
    if text_length >= 20:
        score += 0.25
    elif text_length > 0:
        score += 0.15
    if html_length >= 40:
        score += 0.05
    return min(score, 1.0)


def _default_document_text_extractor(body: bytes, content_type: str | None, url: str) -> str:
    normalized_content_type = (content_type or "").lower()
    if normalized_content_type.startswith("text/"):
        return _decode_body(body, content_type).strip()
    if "pdf" in normalized_content_type or url.split("?", 1)[0].lower().endswith(".pdf"):
        completed = subprocess.run(
            ["pdftotext", "-", "-"],
            input=body,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode == 0:
            return completed.stdout.decode("utf-8", errors="replace").strip()
    return ""


def _default_trafilatura_extractor(html: str, url: str) -> str:
    try:
        import trafilatura  # type: ignore[import-not-found]
    except ImportError:
        return _text_from_html(html)
    extracted = trafilatura.extract(html, url=url)
    return (extracted or "").strip()


def _http_strategy_result(
    *,
    original_url: str,
    normalized_url: str,
    strategy: str,
    policy: dict[str, Any],
    http_fetcher: HttpFetcher,
    document_text_extractor: DocumentTextExtractor,
    trafilatura_extractor: TrafilaturaExtractor,
) -> UniversalCrawlResult:
    started = time.monotonic()
    try:
        response = http_fetcher(normalized_url, _headers_for_strategy(strategy, policy["headers"]), policy["limits"]["timeout_seconds"])
        headers = {key.lower(): value for key, value in response.headers.items()}
        content_type = headers.get("content-type")
        body_text = _decode_body(response.body, content_type)
        is_html = "html" in (content_type or "") or body_text.lstrip().lower().startswith(("<html", "<!doctype html"))
        html = body_text if is_html else None
        if strategy == "DOCUMENT_EXTRACT":
            text = document_text_extractor(response.body, content_type, response.final_url).strip()
        elif strategy == "TRAFILATURA_EXTRACT" and is_html:
            text = trafilatura_extractor(body_text, response.final_url).strip()
        else:
            text = _text_from_html(body_text) if is_html else body_text.strip()
        classification = classify_rendered_page(html, text)
        if response.status_code == 403 and classification == "rendered":
            classification = "access_denied"
        elif response.status_code == 404:
            classification = "not_found"
        elif response.status_code == 410:
            classification = "gone"
        elif response.status_code == 429:
            classification = "rate_limited"
        elif response.status_code >= 500:
            classification = "site_error"
        status = _status_for_classification(classification)
        extraction_method = "http_html" if is_html else "http_body"
        if strategy == "DOCUMENT_EXTRACT":
            extraction_method = "document_extract"
        elif strategy == "TRAFILATURA_EXTRACT":
            extraction_method = "trafilatura_extract"
        quality = _quality_score(status, classification, text, html, response.status_code)
        attempt = CrawlStrategyAttempt(
            strategy=strategy,  # type: ignore[arg-type]
            status=status,  # type: ignore[arg-type]
            classification=classification,  # type: ignore[arg-type]
            http_status=response.status_code,
            content_type=content_type,
            elapsed_ms=response.elapsed_ms,
            error=None if status == "success" else classification,
        )
        return UniversalCrawlResult(
            original_url=original_url,
            normalized_url=normalized_url,
            final_url=response.final_url,
            status=status,  # type: ignore[arg-type]
            classification=classification,  # type: ignore[arg-type]
            selected_strategy=strategy if status == "success" else None,  # type: ignore[arg-type]
            attempted_strategies=[attempt],
            http_status=response.status_code,
            headers=headers,
            content_type=content_type,
            html=html,
            text=text,
            extraction_method=extraction_method,
            quality_score=quality,
            error=None if status == "success" else classification,
        )
    except Exception as error:  # noqa: BLE001 - HTTP clients raise mixed network exceptions
        elapsed_ms = int((time.monotonic() - started) * 1000)
        attempt = CrawlStrategyAttempt(
            strategy=strategy,  # type: ignore[arg-type]
            status="failed",
            classification="network_error",
            http_status=None,
            content_type=None,
            elapsed_ms=elapsed_ms,
            error=str(error),
        )
        return UniversalCrawlResult(
            original_url=original_url,
            normalized_url=normalized_url,
            final_url=None,
            status="failed",
            classification="network_error",
            selected_strategy=None,
            attempted_strategies=[attempt],
            http_status=None,
            headers={},
            content_type=None,
            html=None,
            text=None,
            extraction_method=None,
            quality_score=0.0,
            error=str(error),
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
    )
    job = CrawlJob(index=0, url=normalized_url, domain=registrable_domain(normalized_url), options=options)
    result = await _call_worker(browser_worker, job)
    extraction_method = result.metadata.get("extraction_method") or "dom_text"
    quality = _quality_score(result.status, result.classification, result.text, result.html, None)
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
        text=result.text,
        extraction_method=extraction_method,
        quality_score=quality,
        error=result.error,
    )


async def _execute_crawl_chain(
    *,
    original_url: str,
    normalized_url: str,
    policy: dict[str, Any],
    http_fetcher: HttpFetcher,
    browser_worker: CrawlWorker | None,
    document_text_extractor: DocumentTextExtractor,
    trafilatura_extractor: TrafilaturaExtractor,
) -> UniversalCrawlResult:
    attempts: list[CrawlStrategyAttempt] = []
    last_result: UniversalCrawlResult | None = None
    for strategy in policy["strategies"]:
        if strategy in HTTP_STRATEGIES:
            result = _http_strategy_result(
                original_url=original_url,
                normalized_url=normalized_url,
                strategy=strategy,
                policy=policy,
                http_fetcher=http_fetcher,
                document_text_extractor=document_text_extractor,
                trafilatura_extractor=trafilatura_extractor,
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
        if result.status == "success" and result.quality_score > 0:
            return UniversalCrawlResult(**{**asdict(result), "attempted_strategies": attempts})
    assert last_result is not None
    return UniversalCrawlResult(**{**asdict(last_result), "attempted_strategies": attempts})


async def crawl_request(
    payload: dict[str, Any],
    *,
    settings: CrawlerSettings | None = None,
    browser_worker: CrawlWorker | None = None,
    http_fetcher: HttpFetcher | None = None,
    document_text_extractor: DocumentTextExtractor | None = None,
    trafilatura_extractor: TrafilaturaExtractor | None = None,
) -> dict[str, Any]:
    active_settings = settings or CrawlerSettings.from_mapping()
    urls = _payload_urls(payload)
    if len(urls) > active_settings.max_batch_size:
        raise ValueError("batch_size_exceeded")
    policy = _payload_policy(payload, active_settings)
    fetcher = http_fetcher or _default_http_fetcher
    active_document_text_extractor = document_text_extractor or _default_document_text_extractor
    active_trafilatura_extractor = trafilatura_extractor or _default_trafilatura_extractor

    immediate_results: dict[int, UniversalCrawlResult] = {}
    jobs: list[CrawlExecutionJob] = []
    for index, original_url in enumerate(urls):
        try:
            normalized_url = normalize_public_http_url(original_url)
            domain = registrable_domain(normalized_url)
        except ValueError as error:
            classification = str(error)
            immediate_results[index] = UniversalCrawlResult(
                original_url=original_url,
                normalized_url=None,
                final_url=None,
                status="disallowed",
                classification=classification,  # type: ignore[arg-type]
                selected_strategy=None,
                attempted_strategies=[],
                http_status=None,
                headers={},
                content_type=None,
                html=None,
                text=None,
                extraction_method=None,
                quality_score=0.0,
                error=classification,
            )
            continue
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
            browser_worker=browser_worker,
            document_text_extractor=active_document_text_extractor,
            trafilatura_extractor=active_trafilatura_extractor,
        )
        return index, result

    scheduled_pairs = await scheduler.run(jobs, run_job) if jobs else []
    by_index = dict(immediate_results)
    for index, result in scheduled_pairs:
        by_index[index] = result

    ordered_results = [by_index[index] for index in range(len(urls))]
    return {
        "request_id": f"crawl_{uuid.uuid4().hex[:12]}",
        "submitted_count": len(urls),
        "policy_schema_version": policy["schema_version"],
        "results": [_universal_result_dict(result) for result in ordered_results],
    }


async def render_request(
    payload: dict[str, Any],
    *,
    settings: CrawlerSettings | None = None,
    worker: CrawlWorker,
) -> dict[str, Any]:
    active_settings = settings or CrawlerSettings.from_mapping()
    urls = _payload_urls(payload)
    if len(urls) > active_settings.max_batch_size:
        raise ValueError("batch_size_exceeded")

    options = _render_options(payload, active_settings)
    started = time.monotonic()
    immediate_results: dict[int, CrawlResult] = {}
    jobs: list[CrawlJob] = []

    for index, url in enumerate(urls):
        try:
            normalized = normalize_public_http_url(url)
            domain = registrable_domain(normalized)
        except ValueError as error:
            immediate_results[index] = _disallowed_result((index, url), str(error), int((time.monotonic() - started) * 1000))
            continue
        jobs.append(CrawlJob(index=index, url=normalized, domain=domain, options=options))

    scheduler = DomainScheduler(
        global_limit=active_settings.global_concurrency,
        per_domain_limit=active_settings.per_domain_concurrency,
        delay_seconds=active_settings.per_domain_delay_ms / 1000,
    )
    scheduled_results = await scheduler.run(jobs, lambda job: _call_worker(worker, job)) if jobs else []

    by_index = dict(immediate_results)
    for result in scheduled_results:
        matching_job = next(job for job in jobs if job.url == result.url)
        by_index[matching_job.index] = result

    ordered_results = [by_index[index] for index in range(len(urls))]
    return {
        "request_id": f"crawl_{uuid.uuid4().hex[:12]}",
        "submitted_count": len(urls),
        "results": [_result_dict(result) for result in ordered_results],
    }
