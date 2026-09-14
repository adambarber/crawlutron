"""``/v1/crawl`` URL→handler mapping.

Pure router: imports :func:`app.api.crawl_endpoint.crawl` and
:func:`app.api.crawl_endpoint.get_crawl_result` and binds them to
their paths. The handler implementations live in the endpoint module
so the router stays trivial and the handlers are testable without
FastAPI machinery.
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Body, Depends

from app.api.crawl_endpoint import crawl as crawl_handler
from app.api.crawl_endpoint import get_crawl_result as get_crawl_result_handler
from app.api.dependencies import get_job_store, get_result_store, get_settings, get_shutdown_event
from app.api.policy_defaults import BROWSER_FALLBACK_POLICY, DOCUMENT_EXTRACTION_POLICY
from app.api_models import CrawlerError, CrawlRequest, UnifiedCrawlResponse

router = APIRouter(tags=["Crawler"])


@router.post(
    "/v1/crawl",
    response_model=None,
    summary="Execute a policy-driven crawl",
    description=(
        "Policy is the caller-controlled execution plan for one URL or a batch of URLs. "
        "Use policy.strategies to choose the ordered fallback chain; strategies run in order, the service records "
        "every attempted strategy, and fallback stops at the first successful content result. Available strategy "
        "options (fetch strategies, tried in order) are HTTP_DECLARED_CRAWLER for transparent direct HTTP, "
        "HTTP_CFFI_IMPERSONATE for TLS-fingerprint-impersonated HTTP via curl_cffi with browser-like headers, "
        "BROWSER_HEADLESS for headless Firefox rendering via Camoufox, and BROWSER_HEADED_WARMED for headed Firefox "
        "with warmup URLs. How retrieved content is parsed is controlled separately by policy.extraction (full, "
        "trafilatura, or document); extraction is orthogonal to the fetch strategy. "
        "Returns LLM-friendly condensed Markdown output with metadata for LLM-friendly consumption. "
        "Use policy.limits for timeout_seconds, max_retries, challenge_wait_seconds, "
        "max_discovered_links, and the recursion bounds max_depth and max_pages; omit limits to use defaults from "
        "/v1/capabilities. A synchronous request has no server-side time ceiling - duration scales with max_depth, "
        "max_pages, per-domain politeness delays, and the per-URL total_url_budget_seconds (which fails one tarpit URL, "
        "not the crawl). For large or recursive crawls set async_mode=true (or webhook_url) for an immediate 202 with a "
        "status_url to poll at GET /v1/crawl/{request_id}, or use POST /v1/jobs for durable resumable whole-site jobs. "
        "Within a request the service never crawls the same page twice: submitted URLs are deduped by canonical page "
        "identity, and fetches are reused within the content-cache TTL. Use request_profile for "
        "direct HTTP compatibility options such as user_agent_family, accept_language, fetch metadata, and benign "
        "per-host cookies. Use browser_profile for browser state, viewport, and resource_mode. Use discovery for "
        "robots, sitemaps, and feeds diagnostics. The service records per-URL provenance, selected strategy, raw "
        "evidence, classification evidence, quality score, bounded discovered links, diagnostics.frames for "
        "legacy frameset/iframe URLs, diagnostics.forms for form metadata without submitting forms, and stable "
        "error details. It reports discovered links but does not recursively enqueue them; callers must preserve "
        "form/search state before enqueueing pagination URLs. It classifies denial/challenge outcomes without "
        "CAPTCHA solving, proxy or identity rotation, credential injection, or bypass behavior."
    ),
    responses={
        200: {"model": UnifiedCrawlResponse, "description": "Successful crawl results (JSON output format)."},
        202: {
            "model": CrawlerError,
            "description": "Accepted for async delivery (async_mode or webhook_url); check status_url.",
        },
        422: {"model": CrawlerError, "description": "Invalid crawl request or unsupported output format."},
    },
    openapi_extra={
        "requestBody": {
            "content": {
                "application/json": {
                    "examples": {
                        "browser_fallback": {
                            "summary": "Direct HTTP with browser fallback",
                            "value": {
                                "url": "https://records.example.gov/search?q=permit",
                                "policy": BROWSER_FALLBACK_POLICY,
                            },
                        },
                        "document_extraction": {
                            "summary": "Document extraction policy",
                            "value": {
                                "urls": ["https://records.example.gov/agenda.pdf"],
                                "policy": DOCUMENT_EXTRACTION_POLICY,
                            },
                        },
                    }
                }
            }
        }
    },
)
async def crawl(
    background_tasks: BackgroundTasks,
    payload: CrawlRequest = Body(
        ...,
        examples=[
            {
                "url": "https://records.example.gov/search?q=permit",
                "policy": BROWSER_FALLBACK_POLICY,
            }
        ],
    ),
    settings=Depends(get_settings),
    job_store=Depends(get_job_store),
    result_store=Depends(get_result_store),
    shutdown_event=Depends(get_shutdown_event),
):
    return await crawl_handler(
        payload,
        background_tasks,
        settings=settings,
        job_store=job_store,
        result_store=result_store,
        shutdown_event=shutdown_event,
    )


@router.get(
    "/v1/crawl/{request_id}",
    summary="Retrieve a completed crawl response",
    description=(
        "Return the stored UnifiedCrawlResponse for a previously completed "
        "/v1/crawl request (see status_url in the 202 webhook response). "
        "For async single-shot jobs, reads from the durable job store. "
        "Results are retained for BROWSER_CRAWLER_RESULT_TTL_SECONDS "
        "(default 1h, capped at BROWSER_CRAWLER_RESULT_MAX_ENTRIES entries)."
    ),
)
async def get_crawl_result(
    request_id: str,
    job_store=Depends(get_job_store),
    result_store=Depends(get_result_store),
) -> UnifiedCrawlResponse:
    return await get_crawl_result_handler(request_id, job_store=job_store, result_store=result_store)


__all__ = ["router"]
