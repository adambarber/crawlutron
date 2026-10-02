"""Browser strategy result composition."""

from __future__ import annotations

from typing import Any

from app.api_models import CrawlPolicy
from app.constants import (
    EXTRACTION_METHOD_DOM_TEXT,
)
from app.crawl.render_options import resolve_render_options
from app.crawl.result_helpers import build_engine_result
from app.crawl.strategies.body_decode import quality_score
from app.crawl.strategies.content_pipeline import (
    _discover_after_fetch,
    _discovery_result_fields,
    _ExtractedContent,
    _post_fetch_diagnostics,
    _run_extraction,
)
from app.crawl.strategies.strategy_request import StrategyRequest
from app.crawl.worker import call_worker
from app.models.crawl_job import CrawlJob
from app.models.crawl_result import CrawlStrategyAttempt
from app.models.crawl_worker import CrawlWorker
from app.models.engine_crawl_result import EngineCrawlResult
from app.models.host_policy import HostPolicy
from app.models.registrable_domain import registrable_domain
from app.models.render_options import RenderOptions
from app.models.taxonomy import CrawlStrategyName


def _browser_options(policy: CrawlPolicy) -> RenderOptions:
    return resolve_render_options(
        None,
        {
            "timeout_seconds": policy.limits.timeout_seconds,
            "max_retries": policy.limits.max_retries,
            "respect_robots": True,
            "include_html": True,
            "include_text": True,
            "browser_profile": policy.browser_profile,
        },
    )


def _browser_job(normalized_url: str, options: RenderOptions, host_policy: HostPolicy) -> CrawlJob:
    return CrawlJob(
        index=0,
        url=normalized_url,
        domain=registrable_domain(normalized_url),
        options=options,
        host_policy=host_policy,
    )


async def _extract_browser_content(
    content: str | None,
    result: Any,
    policy: CrawlPolicy,
) -> _ExtractedContent:
    if not content:
        return _ExtractedContent(
            text="",
            markdown=None,
            extraction_method=result.metadata.get("extraction_method") or EXTRACTION_METHOD_DOM_TEXT,
        )
    return await _run_extraction(
        extraction=policy.extraction,
        content=content,
        decoded=content,
        content_type=None,
        final_url=result.final_url,
        policy=policy,
        # A render reports the method its own worker used; the pipeline cannot
        # label that branch for us.
        default_method=result.metadata.get("extraction_method") or EXTRACTION_METHOD_DOM_TEXT,
        html=True,
    )


async def _discover_browser_content(
    result: Any,
    policy: CrawlPolicy,
    host_policy: HostPolicy,
) -> dict[str, Any]:
    return await _discover_after_fetch(
        html=result.html,
        final_url=result.final_url,
        js_base_url=result.final_url,
        status=result.status,
        policy=policy,
        host_policy=host_policy,
    )


def _browser_diagnostics(
    result: Any,
    content: _ExtractedContent,
    policy: CrawlPolicy,
    host_policy: HostPolicy,
    post_fetch: dict[str, Any],
) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {
        "browser_profile": policy.browser_profile,
        "runtime": {"elapsed_ms": result.elapsed_ms, "final_url": result.final_url},
        "content_metrics": {
            "visible_text_length": len(content.text.strip()),
            "html_length": len((result.html or "").strip()),
        },
        **post_fetch["diagnostics"],
    }
    for key in ("intercepted_xhr", "screenshot"):
        value = result.metadata.get(key)
        if value:
            diagnostics[key] = value
    diagnostics.update(_post_fetch_diagnostics(post_fetch, result.final_url, host_policy))
    return diagnostics


def _build_browser_result(
    *,
    original_url: str,
    normalized_url: str,
    strategy: CrawlStrategyName,
    result: Any,
    content: _ExtractedContent,
    diagnostics: dict[str, Any],
    post_fetch: dict[str, Any],
) -> EngineCrawlResult:
    attempt = CrawlStrategyAttempt(
        strategy=strategy,
        status=result.status,
        classification=result.classification,
        http_status=None,
        content_type=None,
        elapsed_ms=result.elapsed_ms,
        error=result.error,
    )
    return build_engine_result(
        original_url=original_url,
        normalized_url=normalized_url,
        final_url=result.final_url,
        status=result.status,
        classification=result.classification,
        selected_strategy=strategy if result.status == "success" else None,
        attempt=attempt,
        http_status=None,
        headers={},
        content_type=None,
        html=result.html,
        text=content.text,
        markdown=content.markdown,
        extraction_method=content.extraction_method,
        quality_score=quality_score(result.status, result.classification, content.text, result.html, None),
        diagnostics=diagnostics,
        **_discovery_result_fields(
            post_fetch,
            classification_evidence=[],
            error=result.error,
        ),
        next_page_url=post_fetch["next_page_url"],
        validators={},
    )


async def browser_strategy_result(
    *,
    request: StrategyRequest,
    browser_worker: CrawlWorker,
) -> EngineCrawlResult:
    result = await call_worker(
        browser_worker,
        _browser_job(request.normalized_url, _browser_options(request.policy), request.host_policy),
    )
    content = result.html if result.html else result.text
    extracted = await _extract_browser_content(content, result, request.policy)
    post_fetch = await _discover_browser_content(result, request.policy, request.host_policy)
    diagnostics = _browser_diagnostics(result, extracted, request.policy, request.host_policy, post_fetch)
    return _build_browser_result(
        original_url=request.original_url,
        normalized_url=request.normalized_url,
        strategy=request.strategy,
        result=result,
        content=extracted,
        diagnostics=diagnostics,
        post_fetch=post_fetch,
    )


__all__ = ["browser_strategy_result"]
