from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Any

from .models import CrawlJob, CrawlResult, RenderOptions
from .scheduler import DomainScheduler
from .settings import CrawlerSettings
from .validation import normalize_public_http_url, registrable_domain


CrawlWorker = Callable[[CrawlJob], Awaitable[CrawlResult] | CrawlResult]


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
