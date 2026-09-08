"""Browser-render request orchestration (the ``/v1/render`` path).

Extracted from the previous ``app.service`` shim so the canonical entry point
for browser-render orchestration lives in ``app.api``. ``app.service`` is
deleted; existing callers import from ``app.api.render``.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from app.crawl.engine import _call_worker, _payload_urls
from app.models import CrawlJob, CrawlResult, CrawlWorker, RenderOptions, to_dict
from app.scheduler import DomainScheduler
from app.settings import CrawlerSettings
from app.validation import normalize_public_http_url, registrable_domain


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
    """Run a /v1/render-shaped request through the per-domain scheduler."""
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
            immediate_results[index] = _disallowed_result(
                (index, url), str(error), int((time.monotonic() - started) * 1000)
            )
            continue
        jobs.append(CrawlJob(index=index, url=normalized, domain=domain, options=options))

    scheduler = DomainScheduler(
        global_limit=active_settings.global_concurrency,
        per_domain_limit=active_settings.per_domain_concurrency,
        delay_seconds=active_settings.per_domain_delay_ms / 1000,
    )
    # The scheduler hands each completed result to on_result together with the
    # submitting job's index, so duplicate URLs in one batch map to their own
    # slots. Matching by URL here would collapse duplicates onto index 0 and
    # KeyError on the missing slot (adversarial H8), and is O(n^2) (L5a).
    by_index = dict(immediate_results)

    async def _collect(index: int, result: CrawlResult) -> None:
        by_index[index] = result

    if jobs:
        await scheduler.run(jobs, lambda job: _call_worker(worker, job), on_result=_collect)

    ordered_results = [by_index[index] for index in range(len(urls))]
    return {
        "request_id": f"crawl_{uuid.uuid4().hex[:12]}",
        "submitted_count": len(urls),
        "results": [to_dict(result) for result in ordered_results],
    }


__all__ = ["render_request", "_render_options", "_disallowed_result"]
