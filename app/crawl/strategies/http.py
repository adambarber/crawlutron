"""HTTP strategy composition and result orchestration.

This module owns the HTTP strategy entry point and coordinates bounded subsystems.
It must not reimplement client, transport, cookie, body, retry, or discovery policy logic.
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass, field, replace
from typing import Any

from app.classify import classify_page_with_evidence
from app.constants import (
    DEFAULT_EXTRACTION,
    DEFAULT_MAX_DISCOVERED_LINKS,
    EXTRACTION_DOCUMENT,
    EXTRACTION_METHOD_HTTP_BODY,
    EXTRACTION_METHOD_HTTP_HTML,
    EXTRACTION_TRAFILATURA,
    status_for_classification,
)
from app.crawl.cache import HttpCache
from app.crawl.content_cache import ContentCache
from app.crawl.decompression import decompress_body as _decompress_body
from app.crawl.document_text import _format_extracted_document_text
from app.crawl.extraction import _is_html
from app.crawl.render_options import policy_wants_html
from app.crawl.result_helpers import (
    _binary_no_text_result,
    _engine_result_from_exception,
    _make_engine_result,
)
from app.crawl.strategies import body_decode, content_cache_key, fetch_policy, request_profile
from app.crawl.strategies.content_pipeline import (
    _discover_after_fetch,
    _discovery_result_fields,
    _post_fetch_diagnostics,
    _run_extraction,
)
from app.crawl.strategies.strategy_request import StrategyRequest
from app.models import (
    Classification,
    CrawlStrategyAttempt,
    CrawlStrategyName,
    EngineCrawlResult,
    HttpFetchResult,
)
from app.validation import HostPolicy


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
    retry_diagnostics: dict[str, Any] = field(default_factory=lambda: {"attempts": 0, "transient_errors": []})


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


@dataclass(frozen=True)
class _FetchedResponse:
    """One fetched response, and the header view every phase reads it through.

    The owner of "how this module keys a response's headers". It exists because
    that rule had three answers. ``_not_modified_result`` built its own
    lowercased copy; ``_http_strategy_result`` built a second one; and
    ``_store_http_caches`` read ``etag``/``last-modified`` off the *raw* map with
    no lowering at all, so it was the one site whose answer depended on the
    transport having already normalized. The fetchers do lowercase today
    (``_finish_fetch``), which is why the third site has never been observed
    wrong -- it is a coincidence, not a contract, and nothing said so.

    What this buys is not tidiness. The phases used to take ``response``,
    ``headers`` and ``content_type`` as three sibling parameters, which made
    "headers from one response, content type from another" a call any of them
    could make and none could detect. Here the header map and the content type
    are read off the response they were derived from, so that pairing is not
    expressible -- there is no way to hand a phase a ``content_type`` that its
    ``headers`` does not contain, because the second is a read of the first.

    Built once per response, where the response enters the phases, so the
    lowercasing is paid once per fetch rather than once per phase that wanted a
    header. Attribute access that is not header access falls through to the
    response unchanged (:meth:`__getattr__`), so this is a header binding and not
    a second response type: it cannot fall behind the transport's shape, and it
    does not need editing when the transport grows a field.
    """

    raw: Any
    headers: dict[str, str]
    content_type: str | None

    @classmethod
    def of(cls, response: Any) -> _FetchedResponse:
        """Bind ``response`` to its headers, keyed for lookup."""
        headers = {key.lower(): value for key, value in response.headers.items()}
        return cls(response, headers, headers.get("content-type"))

    def __getattr__(self, name: str) -> Any:
        # Only reached for names the three fields above do not claim, and
        # ``object.__getattribute__`` rather than ``self.raw`` so that a missing
        # ``raw`` raises AttributeError instead of recursing back into here.
        return getattr(object.__getattribute__(self, "raw"), name)

    @property
    def decompression_failed(self) -> bool:
        """Whether a Content-Encoding promised compression the body never got.

        Stated once because it is asked in two places that must not be able to
        disagree about the same response: the diagnostic that records it, and the
        quality ceiling that caps a result whose body is not what it claims.
        """
        return bool(getattr(self.raw, "decompression_failed", False))

    def validators(self, echoed: dict[str, str] | None = None) -> dict[str, str]:
        """The origin's ETag / Last-Modified, keyed the way a result spells them.

        ``echoed`` is the caller's own conditional headers, read for a validator
        a 304 did not repeat. :data:`app.models.EngineCrawlResult.validators`
        documents ``"etag"``/``"last_modified"``, the response headers are
        ``etag``/``last-modified``, and the caller's are ``If-None-Match``/
        ``If-Modified-Since`` -- three spellings of two headers, which is why
        they are read here rather than at each of the sites that report them.
        """
        fallback = echoed or {}
        return {
            key: value
            for key, value in (
                ("etag", self.headers.get("etag") or fallback.get("If-None-Match")),
                ("last_modified", self.headers.get("last-modified") or fallback.get("If-Modified-Since")),
            )
            if value
        }


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
        ctx.strategy, ctx.policy, ctx.normalized_url, profile=ctx.profile
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


def _not_modified_result(ctx: _HttpStrategyContext, resp: _FetchedResponse) -> EngineCrawlResult | None:
    """Build the terminal result for a negotiated HTTP 304 response."""
    echoed_validators = resp.validators(ctx.caller_conditional)
    sent_conditional = any(key in ctx.request_headers for key in ("If-None-Match", "If-Modified-Since"))
    if resp.status_code != 304 or ctx.revalidated_304 or not sent_conditional:
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
        final_url=resp.final_url,
        status="success",
        classification="not_modified",
        selected_strategy=ctx.strategy,
        attempt=attempt,
        http_status=304,
        headers=resp.headers,
        diagnostics=diagnostics,
        error=None,
        validators=echoed_validators,
    )


async def _store_http_caches(ctx: _HttpStrategyContext, resp: _FetchedResponse) -> None:
    """Store origin validators and fresh content after a successful fetch."""
    if ctx.cache is not None and (resp.headers.get("etag") or resp.headers.get("last-modified")):
        ctx.cache.store(ctx.normalized_url, dict(resp.headers))
    if ctx.content_cache is None or ctx.ckey is None or ctx.content_hit:
        return
    if resp.status_code < 300 and resp.status_code != 304:
        try:
            await ctx.content_cache.aset(ctx.ckey, _serialize_response(resp.raw))
        except Exception:
            pass


def _binary_result_if_needed(ctx: _HttpStrategyContext, resp: _FetchedResponse) -> EngineCrawlResult | None:
    """Return the terminal no-text result for a binary response."""
    if ctx.extraction == EXTRACTION_DOCUMENT or not fetch_policy._looks_like_binary_body(
        resp.body, resp.content_type, resp.final_url
    ):
        return None
    result = _binary_no_text_result(
        original_url=ctx.original_url,
        normalized_url=ctx.normalized_url,
        final_url=resp.final_url,
        strategy=ctx.strategy,
        http_status=resp.status_code,
        headers=resp.headers,
        content_type=resp.content_type,
        elapsed_ms=resp.elapsed_ms,
    )
    # ``replace``, not an ``asdict`` round-trip through the constructor. asdict
    # recurses into the dataclass fields, so it silently converted the frozen
    # CrawlStrategyAttempt list into plain dicts: the copy still compared equal
    # field-for-field and then raised AttributeError the moment a consumer
    # reached for an attribute, which the render adapter does on its first read.
    return replace(result, diagnostics={"retry": ctx.retry_diagnostics})


async def _extract_http_phase(ctx: _HttpStrategyContext, resp: _FetchedResponse) -> _ExtractedHttpContent:
    """Decode the body, run the selected extractor, and measure content.

    ``is_html`` is :func:`app.crawl.extraction._is_html`, the same predicate the
    unified funnel dispatches on, rather than a second reading of the same
    question: this copy asked "does the content type mention html, or does the
    decoded text open like a document" and extraction.py asked "the same", in
    the same package, three functions apart. Two answers to one question about
    one body is how a page that the funnel calls HTML reaches trafilatura here
    and vice versa.
    """
    content_encoding = resp.headers.get("content-encoding", "")
    if content_encoding:
        decompressed = _decompress_body(
            resp.body, content_encoding, max_output_bytes=ctx.max_content_length
        )
        strategy_decompression_failed = False
    else:
        decompressed = resp.body
        strategy_decompression_failed = resp.decompression_failed

    body_text = body_decode._decode_body(decompressed, resp.content_type)
    is_html = _is_html(resp.content_type, body_text)
    html = body_text if is_html else None
    extracted = await _run_extraction(
        extraction=ctx.extraction,
        content=decompressed,
        decoded=body_text,
        content_type=resp.content_type,
        final_url=resp.final_url,
        policy=ctx.policy,
        default_method=EXTRACTION_METHOD_HTTP_HTML if is_html else EXTRACTION_METHOD_HTTP_BODY,
        html=is_html,
    )
    text, markdown = extracted.text, extracted.markdown
    if ctx.extraction == EXTRACTION_DOCUMENT and (resp.content_type or "").lower().startswith("text/"):
        text = _format_extracted_document_text(text, source_url=resp.final_url)
        markdown = text

    return _ExtractedHttpContent(
        decompressed=decompressed,
        body_text=body_text,
        content_type=resp.content_type,
        html=html,
        text=text,
        markdown=markdown,
        extraction_method=extracted.extraction_method,
        strategy_decompression_failed=strategy_decompression_failed,
        content_metrics=body_decode._content_metrics(text, html),
    )


def _request_profile_diagnostics(profile: dict[str, Any]) -> dict[str, str]:
    return {
        "user_agent_family": str(profile.get("user_agent_family", "generic_crawler")),
        "cookie_jar": str(profile.get("cookie_jar", "none")),
    }


def _classify_extracted_content(
    ctx: _HttpStrategyContext,
    resp: _FetchedResponse,
    extracted: _ExtractedHttpContent,
) -> tuple[Classification, list[dict[str, Any]]]:
    """Apply status overrides to the page classifier's evidence."""
    classification, evidence = classify_page_with_evidence(extracted.html, extracted.text)
    if resp.status_code == 403 and classification == "rendered":
        classification = "access_denied"
        evidence = [
            {
                "classification": "access_denied",
                "signature": "403",
                "source": "http_status",
                "context": "HTTP 403",
            }
        ]
    elif resp.status_code == 404:
        classification = "not_found"
        evidence = [
            {
                "classification": "not_found",
                "signature": "404",
                "source": "http_status",
                "context": "HTTP 404",
            }
        ]
    elif resp.status_code == 410:
        classification = "gone"
        evidence = [
            {"classification": "gone", "signature": "410", "source": "http_status", "context": "HTTP 410"}
        ]
    elif resp.status_code == 429:
        classification = "rate_limited"
        evidence = [
            {
                "classification": "rate_limited",
                "signature": "429",
                "source": "http_status",
                "context": "HTTP 429",
            }
        ]
    elif resp.status_code >= 500:
        classification = "site_error"
        evidence = [
            {
                "classification": "site_error",
                "signature": str(resp.status_code),
                "source": "http_status",
                "context": f"HTTP {resp.status_code}",
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
    resp: _FetchedResponse,
    extracted: _ExtractedHttpContent,
) -> EngineCrawlResult:
    """Run discovery shaping and construct the successful strategy result."""
    classification, classification_evidence = _classify_extracted_content(ctx, resp, extracted)
    status = status_for_classification(classification)
    diagnostics: dict[str, Any] = {
        "decompression_failed": extracted.strategy_decompression_failed or resp.decompression_failed,
        "request_profile": _request_profile_diagnostics(ctx.profile),
        "retry": ctx.retry_diagnostics,
        "runtime": {"elapsed_ms": resp.elapsed_ms, "final_url": resp.final_url},
        "content_metrics": extracted.content_metrics,
    }
    discovery = ctx.policy.get("discovery") or {}
    max_depth = int((ctx.policy.get("limits") or {}).get("max_depth") or 0)
    want_discovery_links = bool(discovery) or max_depth > 0
    post_fetch = await _discover_after_fetch(
        html=extracted.html,
        final_url=resp.final_url,
        js_base_url=ctx.normalized_url,
        max_links=ctx.limits.get("max_discovered_links", DEFAULT_MAX_DISCOVERED_LINKS),
        include_diagnostics=bool(discovery),
        include_links=status == "success" and want_discovery_links,
        include_js_endpoints=bool(discovery),
        host_policy=ctx.host_policy,
    )
    if discovery:
        diagnostics.update(post_fetch["diagnostics"])
    retry_after = fetch_policy._retry_after_seconds(resp.headers)
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
    next_page_url = post_fetch["next_page_url"]
    diagnostics.update(_post_fetch_diagnostics(post_fetch, resp.final_url, ctx.host_policy))

    quality = body_decode._quality_score(
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
    attempt = CrawlStrategyAttempt(
        strategy=ctx.strategy,
        status=status,
        classification=classification,
        http_status=resp.status_code,
        content_type=resp.content_type,
        elapsed_ms=resp.elapsed_ms,
        error=error_classification,
    )
    html = extracted.html if ctx.include_html else None
    return _make_engine_result(
        original_url=ctx.original_url,
        normalized_url=ctx.normalized_url,
        final_url=resp.final_url,
        status=status,
        classification=classification,
        selected_strategy=ctx.strategy if status == "success" else None,
        attempt=attempt,
        http_status=resp.status_code,
        headers=resp.headers,
        content_type=resp.content_type,
        html=html,
        text=extracted.text,
        markdown=extracted.markdown,
        extraction_method=extracted.extraction_method,
        quality_score=quality,
        diagnostics=diagnostics,
        **_discovery_result_fields(
            post_fetch,
            classification_evidence=classification_evidence,
            error=error_classification,
        ),
        next_page_url=next_page_url,
        validators=resp.validators(),
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
    request: StrategyRequest,
    http_fetcher: Any,
    cache: HttpCache | None = None,
    content_cache: ContentCache | None = None,
    caller_validators: dict[str, str] | None = None,
    robots_sitemaps: list[str] | None = None,
    global_concurrency: int | None = None,
) -> EngineCrawlResult:
    """Coordinate the named HTTP fetch phases and return one crawl result."""
    ctx = _HttpStrategyContext(
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
    _resolve_http_limits(ctx)
    await _revalidate_content_cache(ctx)
    try:
        if ctx.response is None:
            ctx.response, ctx.retry_diagnostics = await _fetch_http_phase(ctx)
        resp = _FetchedResponse.of(ctx.response)
        not_modified = _not_modified_result(ctx, resp)
        if not_modified is not None:
            return not_modified
        await _store_http_caches(ctx, resp)
        binary_result = _binary_result_if_needed(ctx, resp)
        if binary_result is not None:
            return binary_result
        extracted = await _extract_http_phase(ctx, resp)
        return await _construct_http_result(ctx, resp, extracted)
    except Exception as error:
        return _http_error_result(ctx, error)
