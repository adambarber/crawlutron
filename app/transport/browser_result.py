"""Browser strategy result composition."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from app.admission.render_options import resolve_render_options
from app.models.crawl_diagnostics import CrawlDiagnostics
from app.models.crawl_job import CrawlJob
from app.models.crawl_policy import CrawlPolicy
from app.models.crawl_strategy_attempt import CrawlStrategyAttempt
from app.models.json_value import JsonValue
from app.models.result_factory import build_engine_result
from app.models.taxonomy import CrawlStrategyName
from app.runtime.worker_dispatch import call_worker
from app.transport.body_decode import quality_score
from app.transport.content_pipeline import (
    ExtractedContent,
    PostFetchDiscovery,
    discover_after_fetch,
    discovery_result_fields,
    post_fetch_diagnostics,
    run_extraction,
)
from app.transport.strategy_request import StrategyRequest
from app.vocabulary.extraction import EXTRACTION_METHOD_DOM_TEXT

if TYPE_CHECKING:
    from app.config.settings import CrawlerSettings
    from app.models.crawl_result import CrawlResult, EngineCrawlResult
    from app.models.crawl_worker import CrawlWorker
    from app.models.host_policy import HostPolicy
    from app.models.intercepted_xhr_record import InterceptedXhrRecord
    from app.models.normalized_url import NormalizedUrl
    from app.models.render_options import RenderOptions
    from app.models.screenshot_diagnostics import ScreenshotDiagnostics


def _browser_options(policy: CrawlPolicy) -> RenderOptions:
    return resolve_render_options(
        None,
        {
            "timeout_seconds": policy.limits.timeout_seconds,
            "max_retries": policy.limits.max_retries,
            "respect_robots": True,
            "include_html": True,
            "include_text": True,
            # The policy side holds the record; the resolver's wire edge takes
            # the JsonValue mapping view of it (record values are JsonValue).
            "browser_profile": cast("dict[str, JsonValue]", dict(policy.browser_profile)),
        },
    )


def _browser_job(url: NormalizedUrl, options: RenderOptions, host_policy: HostPolicy) -> CrawlJob:
    return CrawlJob(
        index=0,
        url=url,
        options=options,
        host_policy=host_policy,
    )


def _render_extraction_method(result: CrawlResult) -> str:
    """The method the render's own worker reported; the pipeline cannot label this branch."""
    raw = result.metadata.get("extraction_method")
    return raw if isinstance(raw, str) else EXTRACTION_METHOD_DOM_TEXT


async def _extract_browser_content(
    content: str | None,
    result: CrawlResult,
    policy: CrawlPolicy,
    settings: CrawlerSettings | None = None,
) -> ExtractedContent:
    if not content:
        return ExtractedContent(
            text="",
            markdown=None,
            extraction_method=_render_extraction_method(result),
        )
    return await run_extraction(
        extraction=policy.extraction,
        content=content,
        decoded=content,
        content_type=None,
        final_url=result.final_url,
        policy=policy,
        default_method=_render_extraction_method(result),
        html=True,
        settings=settings,
    )


async def _discover_browser_content(
    result: CrawlResult,
    policy: CrawlPolicy,
    host_policy: HostPolicy,
) -> PostFetchDiscovery:
    return await discover_after_fetch(
        html=result.html,
        final_url=result.final_url,
        js_base_url=result.final_url,
        status=result.status,
        policy=policy,
        host_policy=host_policy,
    )


def _browser_diagnostics(
    result: CrawlResult,
    content: ExtractedContent,
    policy: CrawlPolicy,
    host_policy: HostPolicy,
    post_fetch: PostFetchDiscovery,
) -> CrawlDiagnostics:
    diagnostics: CrawlDiagnostics = {
        "browser_profile": policy.browser_profile,
        "runtime": {
            "elapsed_ms": result.elapsed_ms,
            "final_url": result.final_url.raw if result.final_url else "",
        },
        "content_metrics": {
            "visible_text_length": len(content.text.strip()),
            "html_length": len((result.html or "").strip()),
        },
    }
    # Two disclosure bags by design (parse's, result's); the fold is the
    # boundary. TypedDict.update refuses the cross-bag shape, so cast names it.
    diagnostics.update(cast(CrawlDiagnostics, post_fetch["diagnostics"]))
    # The render worker built these lists as the records named; the metadata bag
    # holds them as JsonValue, so the record view is a producer-boundary cast.
    xhr = result.metadata.get("intercepted_xhr")
    if isinstance(xhr, list):
        diagnostics["intercepted_xhr"] = cast("list[InterceptedXhrRecord]", xhr)
    shots = result.metadata.get("screenshot")
    if isinstance(shots, list):
        diagnostics["screenshot"] = cast("list[ScreenshotDiagnostics]", shots)
    diagnostics.update(post_fetch_diagnostics(post_fetch, result.final_url, host_policy))
    return diagnostics


def _build_browser_result(
    *,
    original_url: str,
    normalized_url: NormalizedUrl,
    strategy: CrawlStrategyName,
    result: CrawlResult,
    content: ExtractedContent,
    diagnostics: CrawlDiagnostics,
    post_fetch: PostFetchDiscovery,
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
        **discovery_result_fields(
            post_fetch,
            classification_evidence=[],
        ),
        next_page_url=post_fetch["next_page_url"],
        validators={},
    )


async def browser_strategy_result(
    *,
    request: StrategyRequest,
    browser_worker: CrawlWorker,
    settings: CrawlerSettings | None = None,
) -> EngineCrawlResult:
    result = await call_worker(
        browser_worker,
        _browser_job(request.normalized_url, _browser_options(request.policy), request.host_policy),
    )
    content = result.html if result.html else result.text
    extracted = await _extract_browser_content(content, result, request.policy, settings)
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
