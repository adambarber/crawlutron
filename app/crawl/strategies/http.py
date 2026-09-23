"""HTTP strategy composition and result orchestration.

This module owns the HTTP strategy entry point and coordinates bounded subsystems.
It must not reimplement client, transport, cookie, body, retry, or discovery policy logic.
"""

from __future__ import annotations

import asyncio
import base64
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from app.classify import classify_page_with_evidence
from app.constants import (
    DEFAULT_EXTRACTION,
    DEFAULT_MAX_DISCOVERED_LINKS,
    EXTRACTION_DOCUMENT,
    EXTRACTION_METHOD_DOCUMENT,
    EXTRACTION_METHOD_HTTP_BODY,
    EXTRACTION_METHOD_HTTP_HTML,
    EXTRACTION_METHOD_TRAFILATURA,
    EXTRACTION_TRAFILATURA,
    status_for_classification,
)
from app.crawl.cache import HttpCache
from app.crawl.content_cache import ContentCache
from app.crawl.decompression import decompress_body as _decompress_body
from app.crawl.decompression import detect_decompression_failure
from app.crawl.document_text import _format_extracted_document_text
from app.crawl.extraction import (
    _extract_document,
    _extract_html,
    extract_content,
    extract_post_fetch_discovery,
)
from app.crawl.render_options import policy_wants_html
from app.crawl.result_helpers import (
    _binary_no_text_result,
    _engine_result_from_exception,
    _make_engine_result,
)
from app.crawl.strategies import body_decode, content_cache_key, fetch_policy, request_profile
from app.models import (
    Classification,
    CrawlStrategyAttempt,
    CrawlStrategyName,
    EngineCrawlResult,
    HttpFetchResult,
)
from app.validation import HostPolicy, filter_js_endpoints


@dataclass
class _HttpStrategyContext:
    original_url: str
    normalized_url: str
    strategy: CrawlStrategyName
    policy: dict[str, Any]
    host_policy: HostPolicy
    http_fetcher: Any
    cache: HttpCache | None
    content_cache: ContentCache | None
    caller_validators: dict[str, str] | None
    robots_sitemaps: list[str] | None
    global_concurrency: int | None
    started: float
    caller_conditional: dict[str, str] = field(default_factory=dict)
    profile: dict[str, Any] = field(default_factory=dict)
    limits: dict[str, Any] = field(default_factory=dict)
    timeout_seconds: int = 10
    max_retries: int = 0
    max_content_length: int = 0
    base_headers: dict[str, str] = field(default_factory=dict)
    request_headers: dict[str, str] = field(default_factory=dict)
    extraction: str = DEFAULT_EXTRACTION
    include_html: bool = False
    robots_evaluated: bool = True
    ckey: str | None = None
    content_hit: bool = False
    revalidated_304: bool = False
    response: Any = None
    retry_diagnostics: dict[str, Any] = field(
        default_factory=lambda: {"attempts": 0, "transient_errors": []}
    )


@dataclass
class _ExtractedHttpContent:
    decompressed: bytes | bytearray
    body_text: str
    content_type: str | None
    html: str | None
    text: str
    markdown: str | None
    extraction_method: str
    strategy_decompression_failed: bool
    content_metrics: dict[str, int | float | bool]


def _serialize_response(resp: Any) -> dict[str, Any]:
    """Capture a fetcher response into a JSON-serializable envelope."""
    return {
        "status_code": int(resp.status_code),
        "headers": dict(resp.headers),
        "body_b64": base64.b64encode(resp.body).decode("ascii"),
        "final_url": str(resp.final_url),
        "elapsed_ms": int(getattr(resp, "elapsed_ms", 0)),
    }


def _rebuild_response(data: dict[str, Any]) -> HttpFetchResult:
    """Reconstruct a response object from a cached envelope."""
    return HttpFetchResult(
        url=data.get("final_url", ""),
        final_url=data["final_url"],
        status_code=data["status_code"],
        headers=data["headers"],
        body=base64.b64decode(data["body_b64"]),
        elapsed_ms=data.get("elapsed_ms", 0),
    )


def _resolve_http_limits(ctx: _HttpStrategyContext) -> None:
    """Resolve request validators, limits, headers, and extraction settings."""
    validators = ctx.caller_validators or {}
    for source, header in (
        ("if_none_match", "If-None-Match"),
        ("if_modified_since", "If-Modified-Since"),
    ):
        value = validators.get(source)
        if value:
            ctx.caller_conditional[header] = value

    ctx.profile = request_profile._validated_request_profile(ctx.policy)
    ctx.limits = ctx.policy.get("limits", {})
    ctx.robots_evaluated = bool(ctx.limits.get("respect_robots", True))
    ctx.timeout_seconds = ctx.limits.get("timeout_seconds", 10)
    ctx.max_retries = ctx.limits.get("max_retries", 0)
    ctx.max_content_length = content_cache_key._effective_max_content_length(
        ctx.limits, ctx.global_concurrency
    )
    ctx.base_headers = request_profile._headers_for_strategy(
        ctx.strategy, ctx.policy, ctx.normalized_url
    )
    ctx.request_headers = dict(ctx.base_headers)
    if ctx.cache is not None:
        ctx.request_headers.update(ctx.cache.conditional_headers(ctx.normalized_url))
    ctx.request_headers.update(ctx.caller_conditional)
    ctx.extraction = ctx.policy.get("extraction", DEFAULT_EXTRACTION)
    ctx.include_html = policy_wants_html(ctx.policy)


async def _revalidate_content_cache(ctx: _HttpStrategyContext) -> None:
    """Serve or revalidate a disk content-cache entry when one is available."""
    if ctx.content_cache is None:
        return
    ckey = content_cache_key._content_cache_key(
        ctx.normalized_url,
        ctx.strategy,
        ctx.base_headers,
        ctx.extraction,
        ctx.policy.get("css_selector"),
        ctx.policy.get("xpath"),
    )
    ctx.ckey = ckey
    cached = await ctx.content_cache.aget(ckey)
    if cached is None:
        return

    validator = ctx.cache.conditional_headers(ctx.normalized_url) if ctx.cache is not None else {}
    if not validator and ctx.caller_conditional:
        validator = dict(ctx.caller_conditional)
    if not validator:
        ctx.response = _rebuild_response(cached)
        ctx.content_hit = True
        ctx.retry_diagnostics = {"attempts": 0, "transient_errors": [], "content_cache": "hit"}
        return

    revalidation_headers = dict(ctx.base_headers)
    revalidation_headers.update(validator)
    revalidation_headers.update(ctx.caller_conditional)
    ctx.response, ctx.retry_diagnostics = await fetch_policy._fetch_with_retries(
        ctx.http_fetcher,
        ctx.normalized_url,
        revalidation_headers,
        ctx.timeout_seconds,
        ctx.max_retries,
        ctx.max_content_length,
    )
    if ctx.response.status_code == 304:
        ctx.response = _rebuild_response(cached)
        ctx.retry_diagnostics = {**ctx.retry_diagnostics, "content_cache": "hit_revalidated"}
        ctx.revalidated_304 = True
    else:
        ctx.retry_diagnostics = {**ctx.retry_diagnostics, "content_cache": "revalidated_fresh"}


async def _fetch_http_phase(ctx: _HttpStrategyContext) -> tuple[Any, dict[str, Any]]:
    """Fetch a response with the strategy retry policy."""
    return await fetch_policy._fetch_with_retries(
        ctx.http_fetcher,
        ctx.normalized_url,
        ctx.request_headers,
        ctx.timeout_seconds,
        ctx.max_retries,
        ctx.max_content_length,
    )


def _not_modified_result(
    ctx: _HttpStrategyContext, response: Any
) -> EngineCrawlResult | None:
    """Build the terminal result for a negotiated HTTP 304 response."""
    response_headers = {key.lower(): value for key, value in response.headers.items()}
    echoed_validators: dict[str, str] = {
        key: value
        for key, value in (
            (
                "etag",
                response_headers.get("etag")
                or (ctx.caller_conditional.get("If-None-Match") if ctx.caller_conditional else None),
            ),
            (
                "last_modified",
                response_headers.get("last-modified")
                or (
                    ctx.caller_conditional.get("If-Modified-Since")
                    if ctx.caller_conditional
                    else None
                ),
            ),
        )
        if value
    }
    sent_conditional = any(
        key in ctx.request_headers for key in ("If-None-Match", "If-Modified-Since")
    )
    if response.status_code != 304 or ctx.revalidated_304 or not sent_conditional:
        return None

    elapsed_ms = int((time.monotonic() - ctx.started) * 1000)
    attempt = CrawlStrategyAttempt(
        strategy=ctx.strategy,
        status="success",
        classification="not_modified",
        http_status=304,
        content_type=None,
        elapsed_ms=elapsed_ms,
        error=None,
    )
    diagnostics: dict[str, Any] = {"retry": ctx.retry_diagnostics}
    if ctx.cache is not None:
        diagnostics["cache"] = "not_modified"
    if ctx.caller_conditional:
        diagnostics["conditional"] = dict(ctx.caller_conditional)
    return _make_engine_result(
        original_url=ctx.original_url,
        normalized_url=ctx.normalized_url,
        final_url=response.final_url,
        status="success",
        classification="not_modified",
        selected_strategy=ctx.strategy,
        attempt=attempt,
        http_status=304,
        headers=response.headers,
        diagnostics=diagnostics,
        error=None,
        validators=echoed_validators,
    )


async def _store_http_caches(ctx: _HttpStrategyContext, response: Any) -> None:
    """Store origin validators and fresh content after a successful fetch."""
    if ctx.cache is not None:
        etag = response.headers.get("etag")
        last_modified = response.headers.get("last-modified")
        if etag or last_modified:
            ctx.cache.store(ctx.normalized_url, dict(response.headers))
    if ctx.content_cache is None or ctx.ckey is None or ctx.content_hit:
        return
    if response.status_code < 300 and response.status_code != 304:
        try:
            await ctx.content_cache.aset(ctx.ckey, _serialize_response(response))
        except Exception:
            pass


def _binary_result_if_needed(
    ctx: _HttpStrategyContext,
    response: Any,
    headers: dict[str, str],
    content_type: str | None,
) -> EngineCrawlResult | None:
    """Return the terminal no-text result for a binary response."""
    if ctx.extraction == EXTRACTION_DOCUMENT or not fetch_policy._looks_like_binary_body(
        response.body, content_type, response.final_url
    ):
        return None
    result = _binary_no_text_result(
        original_url=ctx.original_url,
        normalized_url=ctx.normalized_url,
        final_url=response.final_url,
        strategy=ctx.strategy,
        http_status=response.status_code,
        headers=headers,
        content_type=content_type,
        elapsed_ms=response.elapsed_ms,
    )
    return EngineCrawlResult(**{**asdict(result), "diagnostics": {"retry": ctx.retry_diagnostics}})


async def _extract_http_phase(
    ctx: _HttpStrategyContext,
    response: Any,
    headers: dict[str, str],
    content_type: str | None,
) -> _ExtractedHttpContent:
    """Decode the body, run the selected extractor, and measure content."""
    content_encoding = headers.get("content-encoding", "")
    if content_encoding:
        decompressed = _decompress_body(
            response.body, content_encoding, max_output_bytes=ctx.max_content_length
        )
        strategy_decompression_failed = detect_decompression_failure(
            response.body, content_encoding, decompressed
        )
    else:
        decompressed = response.body
        strategy_decompression_failed = bool(getattr(response, "decompression_failed", False))

    body_text = body_decode._decode_body(decompressed, content_type)
    is_html = "html" in (content_type or "") or body_text.lstrip().lower().startswith(
        ("<html", "<!doctype html")
    )
    html = body_text if is_html else None
    if ctx.extraction == EXTRACTION_DOCUMENT:
        extracted = await asyncio.to_thread(_extract_document, decompressed, response.final_url)
        text = extracted.text or ""
        markdown = extracted.markdown
        if (content_type or "").lower().startswith("text/"):
            text = _format_extracted_document_text(text, source_url=response.final_url)
            markdown = text
        extraction_method = EXTRACTION_METHOD_DOCUMENT
    elif ctx.extraction == EXTRACTION_TRAFILATURA:
        if is_html:
            extracted = await asyncio.to_thread(
                _extract_html,
                body_text,
                response.final_url,
                **_extraction_selector_kwargs(ctx.policy),
            )
            text = extracted.text or ""
            markdown = extracted.markdown
        else:
            text = ""
            markdown = None
        extraction_method = EXTRACTION_METHOD_TRAFILATURA
    else:
        extracted = await asyncio.to_thread(
            extract_content,
            decompressed,
            content_type,
            response.final_url,
            **_extraction_selector_kwargs(ctx.policy),
        )
        text = extracted.text or ""
        markdown = extracted.markdown
        extraction_method = EXTRACTION_METHOD_HTTP_HTML if is_html else EXTRACTION_METHOD_HTTP_BODY

    return _ExtractedHttpContent(
        decompressed=decompressed,
        body_text=body_text,
        content_type=content_type,
        html=html,
        text=text,
        markdown=markdown,
        extraction_method=extraction_method,
        strategy_decompression_failed=strategy_decompression_failed,
        content_metrics=body_decode._content_metrics(text, html),
    )


def _request_profile_diagnostics(profile: dict[str, Any]) -> dict[str, str]:
    return {
        "user_agent_family": str(profile.get("user_agent_family", "generic_crawler")),
        "cookie_jar": str(profile.get("cookie_jar", "none")),
    }


def _extraction_selector_kwargs(policy: dict[str, Any]) -> dict[str, Any]:
    return {
        "css_selector": policy.get("css_selector"),
        "xpath": policy.get("xpath"),
    }


def _classify_extracted_content(
    ctx: _HttpStrategyContext,
    response: Any,
    extracted: _ExtractedHttpContent,
) -> tuple[Classification, list[dict[str, Any]]]:
    """Apply status overrides to the page classifier's evidence."""
    classification, evidence = classify_page_with_evidence(extracted.html, extracted.text)
    if response.status_code == 403 and classification == "rendered":
        classification = "access_denied"
        evidence = [
            {
                "classification": "access_denied",
                "signature": "403",
                "source": "http_status",
                "context": "HTTP 403",
            }
        ]
    elif response.status_code == 404:
        classification = "not_found"
        evidence = [
            {
                "classification": "not_found",
                "signature": "404",
                "source": "http_status",
                "context": "HTTP 404",
            }
        ]
    elif response.status_code == 410:
        classification = "gone"
        evidence = [
            {"classification": "gone", "signature": "410", "source": "http_status", "context": "HTTP 410"}
        ]
    elif response.status_code == 429:
        classification = "rate_limited"
        evidence = [
            {
                "classification": "rate_limited",
                "signature": "429",
                "source": "http_status",
                "context": "HTTP 429",
            }
        ]
    elif response.status_code >= 500:
        classification = "site_error"
        evidence = [
            {
                "classification": "site_error",
                "signature": str(response.status_code),
                "source": "http_status",
                "context": f"HTTP {response.status_code}",
            }
        ]
    if ctx.extraction == EXTRACTION_TRAFILATURA and extracted.html is None and classification == "rendered":
        classification = "binary_no_text"
        evidence = [
            {
                "classification": "binary_no_text",
                "signature": "non_html_body",
                "source": "content_type",
                "context": extracted.content_type or "",
            }
        ]
    return classification, evidence


async def _construct_http_result(
    ctx: _HttpStrategyContext,
    response: Any,
    headers: dict[str, str],
    content_type: str | None,
    extracted: _ExtractedHttpContent,
) -> EngineCrawlResult:
    """Run discovery shaping and construct the successful strategy result."""
    classification, classification_evidence = _classify_extracted_content(ctx, response, extracted)
    status = status_for_classification(classification)
    diagnostics: dict[str, Any] = {
        "decompression_failed": bool(
            extracted.strategy_decompression_failed
            or getattr(response, "decompression_failed", False)
        ),
        "request_profile": _request_profile_diagnostics(ctx.profile),
        "retry": ctx.retry_diagnostics,
        "runtime": {"elapsed_ms": response.elapsed_ms, "final_url": response.final_url},
        "content_metrics": extracted.content_metrics,
    }
    discovery = ctx.policy.get("discovery") or {}
    max_depth = int((ctx.policy.get("limits") or {}).get("max_depth") or 0)
    want_discovery_links = bool(discovery) or max_depth > 0
    post_fetch = await asyncio.to_thread(
        extract_post_fetch_discovery,
        extracted.html,
        response.final_url,
        js_base_url=ctx.normalized_url,
        max_links=ctx.limits.get("max_discovered_links", DEFAULT_MAX_DISCOVERED_LINKS),
        include_diagnostics=bool(discovery),
        include_links=status == "success" and want_discovery_links,
        include_js_endpoints=bool(discovery),
        include_metadata=True,
        host_policy=ctx.host_policy,
    )
    if discovery:
        diagnostics.update(post_fetch["diagnostics"])
    retry_after = fetch_policy._retry_after_seconds(headers)
    if retry_after is not None:
        diagnostics["cooldown"] = {"retry_after_seconds": retry_after, "scope": "domain"}
    if discovery:
        diagnostics.update(
            await fetch_policy._discovery_diagnostics(
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
    if max_depth > 0:
        discovery_diag = diagnostics.get("discovery")
        if discovery_diag:
            discovery_diag.pop("sitemap_urls", None)
            discovery_diag.pop("feeds", None)
    diagnostics["js_endpoints"] = filter_js_endpoints(
        post_fetch["js_endpoints"],
        resolve_against=response.final_url,
        policy=ctx.host_policy,
    )
    next_page_url = post_fetch["next_page_url"]
    if next_page_url:
        diagnostics["next_page_url"] = next_page_url

    quality = body_decode._quality_score(
        status,
        classification,
        extracted.text,
        extracted.html,
        response.status_code,
        content_metrics=extracted.content_metrics,
    )
    if extracted.strategy_decompression_failed or getattr(response, "decompression_failed", False):
        quality = min(quality, 0.25)
    origin_validators = {
        key: value
        for key, value in (
            ("etag", headers.get("etag")),
            ("last_modified", headers.get("last-modified")),
        )
        if value
    }
    if (
        status == "success"
        and not extracted.strategy_decompression_failed
        and extracted.content_metrics["visible_text_length"] >= 40
    ):
        quality = max(quality, 0.95)

    error_classification: Classification | None = None if status == "success" else classification
    attempt = CrawlStrategyAttempt(
        strategy=ctx.strategy,
        status=status,
        classification=classification,
        http_status=response.status_code,
        content_type=content_type,
        elapsed_ms=response.elapsed_ms,
        error=error_classification,
    )
    html = extracted.html if ctx.include_html else None
    return _make_engine_result(
        original_url=ctx.original_url,
        normalized_url=ctx.normalized_url,
        final_url=response.final_url,
        status=status,
        classification=classification,
        selected_strategy=ctx.strategy if status == "success" else None,
        attempt=attempt,
        http_status=response.status_code,
        headers=headers,
        content_type=content_type,
        html=html,
        text=extracted.text,
        llm_metadata=post_fetch["llm_metadata"] or None,
        markdown=extracted.markdown,
        extraction_method=extracted.extraction_method,
        quality_score=quality,
        classification_evidence=classification_evidence,
        diagnostics=diagnostics,
        discovered_link_count=post_fetch["discovered_link_count"],
        discovered_links_truncated=post_fetch["discovered_links_truncated"],
        discovered_links=post_fetch["discovered_links"],
        error=error_classification,
        next_page_url=next_page_url,
        validators=origin_validators,
    )


def _http_error_result(ctx: _HttpStrategyContext, error: Exception) -> EngineCrawlResult:
    """Build the redacted terminal result for a strategy exception."""
    elapsed_ms = int((time.monotonic() - ctx.started) * 1000)
    return _engine_result_from_exception(
        error=error,
        original_url=ctx.original_url,
        normalized_url=ctx.normalized_url,
        strategy=ctx.strategy,
        elapsed_ms=elapsed_ms,
        extra_diagnostics={
            "retry": ctx.retry_diagnostics,
            "runtime": {"elapsed_ms": elapsed_ms},
            "request_profile": _request_profile_diagnostics(ctx.profile),
        },
    )


async def _http_strategy_result(
    *,
    original_url: str,
    normalized_url: str,
    strategy: CrawlStrategyName,
    policy: dict[str, Any],
    host_policy: HostPolicy,
    http_fetcher: Any,
    cache: HttpCache | None = None,
    content_cache: ContentCache | None = None,
    caller_validators: dict[str, str] | None = None,
    robots_sitemaps: list[str] | None = None,
    global_concurrency: int | None = None,
) -> EngineCrawlResult:
    """Coordinate the named HTTP fetch phases and return one crawl result."""
    ctx = _HttpStrategyContext(
        original_url=original_url,
        normalized_url=normalized_url,
        strategy=strategy,
        policy=policy,
        host_policy=host_policy,
        http_fetcher=http_fetcher,
        cache=cache,
        content_cache=content_cache,
        caller_validators=caller_validators,
        robots_sitemaps=robots_sitemaps,
        global_concurrency=global_concurrency,
        started=time.monotonic(),
    )
    _resolve_http_limits(ctx)
    await _revalidate_content_cache(ctx)
    try:
        if ctx.response is None:
            ctx.response, ctx.retry_diagnostics = await _fetch_http_phase(ctx)
        response = ctx.response
        not_modified = _not_modified_result(ctx, response)
        if not_modified is not None:
            return not_modified
        await _store_http_caches(ctx, response)
        headers = {key.lower(): value for key, value in response.headers.items()}
        content_type = headers.get("content-type")
        binary_result = _binary_result_if_needed(ctx, response, headers, content_type)
        if binary_result is not None:
            return binary_result
        extracted = await _extract_http_phase(ctx, response, headers, content_type)
        return await _construct_http_result(ctx, response, headers, content_type, extracted)
    except Exception as error:
        return _http_error_result(ctx, error)
