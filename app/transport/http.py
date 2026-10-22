"""HTTP strategy entry: fetch, cache, extract and construct the result."""

from __future__ import annotations

import time
from dataclasses import replace
from typing import Any

from app.admission.policy import discovery_flags
from app.models.crawl_diagnostics import CrawlDiagnostics
from app.models.crawl_result import EngineCrawlResult
from app.models.crawl_strategy_attempt import CrawlStrategyAttempt
from app.models.fetch_envelope import RetryDiagnostics
from app.models.result_factory import binary_no_text_result, build_engine_result
from app.models.taxonomy import Classification
from app.orchestration.results import engine_result_from_exception
from app.transport import body_decode, fetch_policy
from app.transport.cache import HttpCache
from app.transport.content_cache import ContentCache
from app.transport.content_pipeline import (
    discover_after_fetch,
    discovery_result_fields,
    post_fetch_diagnostics,
)
from app.transport.http_cache import (
    not_modified_result,
    resolve_http_limits,
    revalidate_content_cache,
    store_http_caches,
)
from app.transport.http_classify import (
    classify_extracted_content,
    extract_http_phase,
    request_profile_diagnostics,
)
from app.transport.http_response import (
    ExtractedHttpContent,
    FetchedResponse,
    HttpStrategyContext,
    response_result_fields,
)
from app.transport.strategy_request import StrategyRequest
from app.vocabulary.extraction import EXTRACTION_DOCUMENT
from app.vocabulary.taxonomy import status_for_classification


async def _fetch_http_phase(ctx: HttpStrategyContext) -> tuple[Any, RetryDiagnostics]:
    """Fetch a response with the strategy retry policy."""
    return await fetch_policy.fetch_with_retries(
        ctx.http_fetcher,
        ctx.normalized_url,
        ctx.request_headers,
        ctx.timeout_seconds,
        ctx.max_retries,
        ctx.max_content_length,
    )


def _binary_result_if_needed(ctx: HttpStrategyContext, resp: FetchedResponse) -> EngineCrawlResult | None:
    """Return the terminal no-text result for a binary response."""
    if ctx.extraction == EXTRACTION_DOCUMENT or not fetch_policy.looks_like_binary_body(
        resp.body, resp.content_type, resp.final_url
    ):
        return None
    result = binary_no_text_result(
        original_url=ctx.original_url,
        normalized_url=ctx.normalized_url,
        final_url=resp.final_url,
        strategy=ctx.strategy,
        http_status=resp.status_code,
        headers=resp.headers,
        content_type=resp.content_type,
        elapsed_ms=resp.elapsed_ms,
    )
    # ``replace``, not an ``asdict`` round-trip: asdict converts the frozen
    # CrawlStrategyAttempt list into plain dicts, which then fails on first read.
    return replace(result, diagnostics={"retry": ctx.retry_diagnostics})


async def _construct_http_result(
    ctx: HttpStrategyContext,
    resp: FetchedResponse,
    extracted: ExtractedHttpContent,
) -> EngineCrawlResult:
    """Run discovery shaping and construct the successful strategy result."""
    classification, classification_evidence = classify_extracted_content(ctx, resp, extracted)
    # A content-cache revalidation that earned a 304 re-serves the cached body; it is not a fresh
    # render, so report not_modified (kept content lets WatchSweeper diff, not fire a webhook).
    revalidated_304 = ctx.revalidated_304
    if revalidated_304:
        classification = "not_modified"
        classification_evidence = []
    status = status_for_classification(classification)
    diagnostics: CrawlDiagnostics = {
        "decompression_failed": extracted.strategy_decompression_failed or resp.decompression_failed,
        "request_profile": request_profile_diagnostics(ctx.profile),
        "retry": ctx.retry_diagnostics,
        "runtime": {"elapsed_ms": resp.elapsed_ms, "final_url": resp.final_url},
        "content_metrics": extracted.content_metrics,
    }
    # "Any channel on", not "the block exists": with channels off by default,
    # an all-off block is the empty mapping that ``is not None`` now distinguishes.
    post_fetch = await discover_after_fetch(
        html=extracted.html,
        final_url=resp.final_url,
        js_base_url=ctx.normalized_url,
        status=status,
        policy=ctx.policy,
        host_policy=ctx.host_policy,
    )
    discovery_block = any(discovery_flags(ctx.policy))
    if discovery_block:
        diagnostics.update(post_fetch["diagnostics"])
    retry_after = fetch_policy.retry_after_seconds(resp.headers)
    if retry_after is not None:
        diagnostics["cooldown"] = {"retry_after_seconds": retry_after, "scope": "domain"}
    if discovery_block:
        diagnostics.update(
            await fetch_policy.discovery_diagnostics(
                normalized_url=ctx.normalized_url,
                engine_robots_sitemaps=ctx.robots_sitemaps,
                robots_evaluated=ctx.robots_evaluated,
                html=extracted.html,
                policy=ctx.policy,
                host_policy=ctx.host_policy,
                headers=ctx.base_headers,
                http_fetcher=ctx.http_fetcher,
            )
        )
    if ctx.policy.limits.max_depth > 0:
        discovery_diag = diagnostics.get("discovery")
        if discovery_diag:
            discovery_diag.pop("sitemap_urls", None)
            discovery_diag.pop("feeds", None)
    next_page_url = post_fetch["next_page_url"]
    diagnostics.update(post_fetch_diagnostics(post_fetch, resp.final_url, ctx.host_policy))

    quality = body_decode.quality_score(
        status,
        classification,
        extracted.text,
        extracted.html,
        resp.status_code,
        content_metrics=extracted.content_metrics,
    )
    if extracted.strategy_decompression_failed or resp.decompression_failed:
        quality = min(quality, 0.25)
    if (
        status == "success"
        and not extracted.strategy_decompression_failed
        and extracted.content_metrics["visible_text_length"] >= 40
    ):
        quality = max(quality, 0.95)

    error_classification: Classification | None = None if status == "success" else classification
    result_http_status = 304 if revalidated_304 else resp.status_code
    attempt = CrawlStrategyAttempt(
        strategy=ctx.strategy,
        status=status,
        classification=classification,
        http_status=result_http_status,
        content_type=resp.content_type,
        elapsed_ms=resp.elapsed_ms,
        error=error_classification,
    )
    html = extracted.html if ctx.include_html else None
    result = build_engine_result(
        **response_result_fields(ctx, resp, attempt),
        selected_strategy=ctx.strategy if status == "success" else None,
        http_status=result_http_status,
        content_type=resp.content_type,
        html=html,
        text=extracted.text,
        markdown=extracted.markdown,
        extraction_method=extracted.extraction_method,
        quality_score=quality,
        diagnostics=diagnostics,
        **discovery_result_fields(
            post_fetch,
            classification_evidence=classification_evidence,
        ),
        next_page_url=next_page_url,
        validators=resp.validators(),
    )
    return result


def _http_error_result(ctx: HttpStrategyContext, error: Exception) -> EngineCrawlResult:
    """Build the redacted terminal result for a strategy exception."""
    elapsed_ms = int((time.monotonic() - ctx.started) * 1000)
    return engine_result_from_exception(
        error=error,
        original_url=ctx.original_url,
        normalized_url=ctx.normalized_url,
        strategy=ctx.strategy,
        elapsed_ms=elapsed_ms,
        extra_diagnostics={
            "retry": ctx.retry_diagnostics,
            "runtime": {"elapsed_ms": elapsed_ms},
            "request_profile": request_profile_diagnostics(ctx.profile),
        },
    )


async def http_strategy_result(
    *,
    request: StrategyRequest,
    http_fetcher: Any,
    cache: HttpCache | None = None,
    content_cache: ContentCache | None = None,
    caller_validators: dict[str, str] | None = None,
    robots_sitemaps: list[str] | None = None,
    global_concurrency: int | None = None,
) -> EngineCrawlResult:
    """Coordinate the named HTTP fetch phases and return one crawl result."""
    ctx = HttpStrategyContext(
        original_url=request.original_url,
        normalized_url=request.normalized_url,
        strategy=request.strategy,
        policy=request.policy,
        host_policy=request.host_policy,
        http_fetcher=http_fetcher,
        cache=cache,
        content_cache=content_cache,
        caller_validators=caller_validators,
        robots_sitemaps=robots_sitemaps,
        global_concurrency=global_concurrency,
        started=time.monotonic(),
    )
    # The resolver's jar read is SQLite when persistence is on: awaited, off-loop.
    await resolve_http_limits(ctx)
    await revalidate_content_cache(ctx)
    try:
        if ctx.response is None:
            ctx.response, ctx.retry_diagnostics = await _fetch_http_phase(ctx)
        resp = FetchedResponse.of(ctx.response)
        not_modified = not_modified_result(ctx, resp)
        if not_modified is not None:
            return not_modified
        await store_http_caches(ctx, resp)
        binary_result = _binary_result_if_needed(ctx, resp)
        if binary_result is not None:
            return binary_result
        extracted = await extract_http_phase(ctx, resp)
        return await _construct_http_result(ctx, resp, extracted)
    except Exception as error:
        return _http_error_result(ctx, error)
