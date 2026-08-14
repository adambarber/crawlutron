"""Service orchestration entry points.

The crawl-execution logic has been modularized into ``app.crawl``:

* :mod:`app.crawl.engine`  - ``crawl_request`` / ``_execute_crawl_chain`` / ``_call_worker``
* :mod:`app.crawl.strategies.http`    - HTTP fetch strategy + helpers
* :mod:`app.crawl.strategies.browser` - browser render strategy (``BrowserRenderer``)
* :mod:`app.crawl.strategies.document`- PDF/Office extraction
* :mod:`app.crawl.extraction` - unified content extraction + discovery diagnostics

This module re-exports those symbols so existing callers/tests that import
from ``app.service`` keep working, and additionally owns the browser-only
``/v1/render`` path (``render_request``).
"""

from __future__ import annotations

import asyncio
import time
import urllib.error
import uuid
from typing import Any

from app.validation import normalize_public_http_url, registrable_domain
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Any

from app.crawl.engine import (
    CrawlExecutionJob,
    _call_worker,
    _execute_crawl_chain,
    _payload_policy,
    _payload_urls,
    _universal_result_dict,
    crawl_request,
)
from app.crawl.extraction import (
    _extract_discovered_links,
    _extract_forms,
    _extract_frames,
    _FrameCollector,
    _html_diagnostics,
)
from app.crawl.strategies.http import (
    _cookie_header_for_url,
    _default_cffi_fetcher,
    _default_http_fetcher,
    _document_item_range,
    _fetch_with_retries,
    _format_extracted_document_text,
    _headers_for_strategy,
    _http_strategy_result,
    _looks_like_binary_body,
    _next_non_empty_document_line,
    _retry_after_seconds,
    _should_join_document_paragraph,
    _collect_document_item_lines,
)
from app.models import CrawlJob, CrawlResult, RenderOptions
from app.scheduler import DomainScheduler
from app.settings import CrawlerSettings


CrawlWorker = Callable[[CrawlJob], Awaitable[CrawlResult] | CrawlResult]


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




def _result_dict(result: CrawlResult) -> dict[str, Any]:
    return asdict(result)
