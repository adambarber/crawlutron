"""HTTP strategy composition and result orchestration.

This module owns the HTTP strategy entry point and coordinates bounded subsystems.
It must not reimplement client, transport, cookie, body, retry, or discovery policy logic.
"""

from __future__ import annotations

import asyncio
import base64
import time
from dataclasses import asdict
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
    CrawlStrategyAttempt,
    CrawlStrategyName,
    EngineCrawlResult,
    HttpFetchResult,
)
from app.validation import HostPolicy, filter_js_endpoints


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
    """Reconstruct a minimal response object from a cached envelope.

    The rebuilt object carries exactly the fields the caller reads
    (``status_code``/``headers``/``body``/``final_url``/``elapsed_ms``) as a
    declared dataclass: the previous inline class had no fields at all, so the
    five attribute assignments were the only definition and every reader was
    an untyped ``Any`` access. ``HttpFetchResult`` is the fetcher's real result
    shape and matches field-for-field, including the optional
    ``decompression_failed`` flag readers check via getattr.
    """
    return HttpFetchResult(
        url=data.get("final_url", ""),
        final_url=data["final_url"],
        status_code=data["status_code"],
        headers=data["headers"],
        body=base64.b64decode(data["body_b64"]),
        elapsed_ms=data.get("elapsed_ms", 0),
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
    started = time.monotonic()
    retry_diagnostics: dict[str, Any] = {"attempts": 0, "transient_errors": []}
    # Caller-supplied conditional validators (freshness monitoring): sent as
    # If-None-Match / If-Modified-Since so an unchanged page returns 304.
    caller_conditional: dict[str, str] = {}
    if caller_validators:
        if caller_validators.get("if_none_match"):
            caller_conditional["If-None-Match"] = caller_validators["if_none_match"]
        if caller_validators.get("if_modified_since"):
            caller_conditional["If-Modified-Since"] = caller_validators["if_modified_since"]
    # Apply policy limits; fall back to strategy defaults when the caller
    # omitted them (engine-provided policies always carry explicit limits).
    limits = policy.get("limits", {})
    robots_evaluated = bool(limits.get("respect_robots", True))
    timeout_seconds = limits.get("timeout_seconds", 10)
    max_retries = limits.get("max_retries", 0)
    # The engine threads its live concurrency here (perf throughput #5) so the
    # RAM-scaled cap tracks real dispatch instead of the settings floor.
    max_content_length = content_cache_key._effective_max_content_length(limits, global_concurrency)
    base_headers = request_profile._headers_for_strategy(strategy, policy, normalized_url)
    request_headers = dict(base_headers)
    if cache is not None:
        request_headers.update(cache.conditional_headers(normalized_url))
    if caller_conditional:
        request_headers.update(caller_conditional)

    extraction = policy.get("extraction", DEFAULT_EXTRACTION)
    # Drop the raw HTML as early as possible when the caller did not ask for it
    # so the decoded string + lxml tree are not retained in the result (T3/#2.1).
    include_html = policy_wants_html(policy)

    # Disk content cache (#5): serve an unchanged URL from local disk instead of
    # re-downloading. The key is derived from the *base* request headers (excluding
    # HTTP conditional-revalidation headers) so it stays stable across crawls -- the
    # conditional headers only change the 304 handshake, never the payload.
    #
    # ``Cookie`` is the exception and STAYS in the key (P2-G): the cookie namespace
    # is what separates one tenant's authenticated view of a URL from another's, so
    # a key that dropped it could serve tenant A's private page to tenant B from A's
    # cache slot. Differing credentials must never share an entry.
    ckey: str | None = None
    # ``content_hit`` means exactly ONE thing: the served body was assembled
    # from the content cache, so the store below must not rewrite it. Whether
    # a response object is in hand is DATAFLOW (``response is None``), not a
    # second meaning of this flag -- reusing it for both (double-fetch fix,
    # 2026-09-22) silently skipped storing origin-fresh revalidations: every
    # later crawl re-downloaded the body forever. Pinned in
    # tests/test_revalidation_fetch.py.
    content_hit = False
    revalidated_304 = False
    response: Any = None
    if content_cache is not None:
        ckey = content_cache_key._content_cache_key(
            normalized_url,
            strategy,
            base_headers,
            extraction,
            policy.get("css_selector"),
            policy.get("xpath"),
        )
        cached = await content_cache.aget(ckey)
        if cached is not None:
            validator = cache.conditional_headers(normalized_url) if cache is not None else {}
            if not validator and caller_conditional:
                # Caller-supplied validators must win over serving a cached body:
                # silently ignoring If-None-Match serves stale content as success.
                validator = dict(caller_conditional)
            if not validator:
                # No validator to re-check against -> serve cached payload directly.
                # Staleness is bounded by the cache TTL (acceptable for etag-less pages).
                response = _rebuild_response(cached)
                content_hit = True
                retry_diagnostics = {"attempts": 0, "transient_errors": [], "content_cache": "hit"}
            else:
                # A validator exists -> revalidate with a cheap conditional GET so we
                # never serve stale content for an etagged/last-modified URL.
                reval_headers = dict(base_headers)
                reval_headers.update(validator)
                reval_headers.update(caller_conditional)
                response, retry_diagnostics = await fetch_policy._fetch_with_retries(
                    http_fetcher,
                    normalized_url,
                    reval_headers,
                    timeout_seconds,
                    max_retries,
                    max_content_length,
                )
                if response.status_code == 304:
                    response = _rebuild_response(cached)
                    retry_diagnostics = {**retry_diagnostics, "content_cache": "hit_revalidated"}
                    revalidated_304 = True
                else:
                    # The origin answered 200 (content changed): this response
                    # is the fresh one. It is already in hand (no second fetch
                    # -- response is not None below), it is NOT served from the
                    # cache (content_hit stays False), and the store gate
                    # therefore writes it back so later crawls skip the origin.
                    retry_diagnostics = {
                        **retry_diagnostics,
                        "content_cache": "revalidated_fresh",
                    }

    try:
        if response is None:
            response, retry_diagnostics = await fetch_policy._fetch_with_retries(
                http_fetcher,
                normalized_url,
                request_headers,
                timeout_seconds,
                max_retries,
                max_content_length,
            )

        # HTTP conditional caching: a 304 from a real fetch means the page is unchanged.
        # (A revalidation-304 already had its cached body restored above, so it must
        #  NOT be reported as not_modified -- we serve the cached content instead.)
        # Caller-supplied validators (freshness monitoring) also produce a
        # not_modified result on 304: no body exists for caller-owned validators.
        # A 304 we did not negotiate (no If-* header was sent) is honored by the
        # letter of HTTP but is trivially attacker-controlled content suppression;
        # treat it as a normal response body instead.
        resp_headers_304 = {key.lower(): value for key, value in response.headers.items()}
        echoed_validators: dict[str, str] = {
            key: value
            for key, value in (
                (
                    "etag",
                    resp_headers_304.get("etag")
                    or (caller_conditional.get("If-None-Match") if caller_conditional else None),
                ),
                (
                    "last_modified",
                    resp_headers_304.get("last-modified")
                    or (caller_conditional.get("If-Modified-Since") if caller_conditional else None),
                ),
            )
            if value
        }
        sent_conditional = any(key in request_headers for key in ("If-None-Match", "If-Modified-Since"))
        if response.status_code == 304 and not revalidated_304 and sent_conditional:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            attempt = CrawlStrategyAttempt(
                strategy=strategy,
                status="success",
                classification="not_modified",
                http_status=304,
                content_type=None,
                elapsed_ms=elapsed_ms,
                error=None,
            )
            diagnostics_304: dict[str, Any] = {"retry": retry_diagnostics}
            if cache is not None:
                diagnostics_304["cache"] = "not_modified"
            if caller_conditional:
                diagnostics_304["conditional"] = dict(caller_conditional)
            return _make_engine_result(
                original_url=original_url,
                normalized_url=normalized_url,
                final_url=response.final_url,
                status="success",
                classification="not_modified",
                selected_strategy=strategy,
                attempt=attempt,
                http_status=304,
                headers=response.headers,
                diagnostics=diagnostics_304,
                error=None,
                validators=echoed_validators,
            )

        if cache is not None:
            etag = response.headers.get("etag")
            last_modified = response.headers.get("last-modified")
            if etag or last_modified:
                cache.store(normalized_url, dict(response.headers))

        if content_cache is not None and ckey is not None and not content_hit:
            if response.status_code < 300 and response.status_code != 304:
                try:
                    await content_cache.aset(ckey, _serialize_response(response))
                except Exception:
                    pass

        headers = {key.lower(): value for key, value in response.headers.items()}
        content_type = headers.get("content-type")
        if policy.get("extraction") != EXTRACTION_DOCUMENT and fetch_policy._looks_like_binary_body(
            response.body, content_type, response.final_url
        ):
            result = _binary_no_text_result(
                original_url=original_url,
                normalized_url=normalized_url,
                final_url=response.final_url,
                strategy=strategy,
                http_status=response.status_code,
                headers=headers,
                content_type=content_type,
                elapsed_ms=response.elapsed_ms,
            )
            return EngineCrawlResult(**{**asdict(result), "diagnostics": {"retry": retry_diagnostics}})
        content_encoding_header = headers.get("content-encoding", "")
        if content_encoding_header:
            # The fetcher normally decompresses and strips this header; if it is
            # still present the body was not verified -- detect silent failure.
            decompressed = _decompress_body(
                response.body, content_encoding_header, max_output_bytes=max_content_length
            )
            strategy_decompression_failed = detect_decompression_failure(
                response.body, content_encoding_header, decompressed
            )
        else:
            decompressed = response.body
            strategy_decompression_failed = bool(getattr(response, "decompression_failed", False))
        body_text = body_decode._decode_body(decompressed, content_type)
        is_html = "html" in (content_type or "") or body_text.lstrip().lower().startswith(
            ("<html", "<!doctype html")
        )
        html = body_text if is_html else None
        if extraction == EXTRACTION_DOCUMENT:
            extracted = await asyncio.to_thread(_extract_document, decompressed, response.final_url)
            text = extracted.text or ""
            markdown = extracted.markdown
            if (content_type or "").lower().startswith("text/"):
                text = _format_extracted_document_text(text, source_url=response.final_url)
                markdown = text
            extraction_method = EXTRACTION_METHOD_DOCUMENT
        elif extraction == EXTRACTION_TRAFILATURA:
            if is_html:
                # trafilatura is CPU-bound: keep it off the event loop like the
                # neighboring document/full-pipeline extractions (perf throughput #3).
                extracted = await asyncio.to_thread(
                    _extract_html,
                    body_text,
                    response.final_url,
                    css_selector=policy.get("css_selector"),
                    xpath=policy.get("xpath"),
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
                css_selector=policy.get("css_selector"),
                xpath=policy.get("xpath"),
            )
            text = extracted.text or ""
            markdown = extracted.markdown
            extraction_method = EXTRACTION_METHOD_HTTP_HTML if is_html else EXTRACTION_METHOD_HTTP_BODY
        classification, classification_evidence = classify_page_with_evidence(html, text)
        if response.status_code == 403 and classification == "rendered":
            classification = "access_denied"
            classification_evidence = [
                {
                    "classification": "access_denied",
                    "signature": "403",
                    "source": "http_status",
                    "context": "HTTP 403",
                }
            ]
        elif response.status_code == 404:
            classification = "not_found"
            classification_evidence = [
                {
                    "classification": "not_found",
                    "signature": "404",
                    "source": "http_status",
                    "context": "HTTP 404",
                }
            ]
        elif response.status_code == 410:
            classification = "gone"
            classification_evidence = [
                {"classification": "gone", "signature": "410", "source": "http_status", "context": "HTTP 410"}
            ]
        elif response.status_code == 429:
            classification = "rate_limited"
            classification_evidence = [
                {
                    "classification": "rate_limited",
                    "signature": "429",
                    "source": "http_status",
                    "context": "HTTP 429",
                }
            ]
        elif response.status_code >= 500:
            classification = "site_error"
            classification_evidence = [
                {
                    "classification": "site_error",
                    "signature": str(response.status_code),
                    "source": "http_status",
                    "context": f"HTTP {response.status_code}",
                }
            ]
        if extraction == EXTRACTION_TRAFILATURA and not is_html and classification == "rendered":
            # De-aliased from EXTRACTION_METHOD_BINARY_NO_TEXT: that is an
            # extraction-method constant whose value happens to equal this
            # classification member today, but the two namespaces are
            # unrelated, so writing one into a ``classification`` slot is the
            # shape that silently crosses a declared type boundary.
            classification = "binary_no_text"
            classification_evidence = [
                {
                    "classification": "binary_no_text",
                    "signature": "non_html_body",
                    "source": "content_type",
                    "context": content_type or "",
                }
            ]
        status = status_for_classification(classification)
        diagnostics: dict[str, Any] = {
            "decompression_failed": bool(
                strategy_decompression_failed or getattr(response, "decompression_failed", False)
            ),
            "request_profile": {
                "user_agent_family": str(
                    policy.get("request_profile", {}).get("user_agent_family", "generic_crawler")
                ),
                "cookie_jar": str(policy.get("request_profile", {}).get("cookie_jar", "none")),
            },
            "retry": retry_diagnostics,
            "runtime": {"elapsed_ms": response.elapsed_ms, "final_url": response.final_url},
            "content_metrics": (content_metrics := body_decode._content_metrics(text, html)),
        }
        # Discovery is only requested when the caller opts in; recursive crawls
        # (max_depth > 0) also need discovered links to expand the frontier.
        discovery = policy.get("discovery") or {}
        _max_depth = int((policy.get("limits") or {}).get("max_depth") or 0)
        _want_discovery_links = bool(discovery) or _max_depth > 0
        # Post-fetch discovery (perf #9): the lxml parse + link/frame/form/
        # JS/pagination passes run in a worker thread (single parse, shared
        # tree inside). Only plain data returns; egress filtering below is
        # unchanged. Frames/forms stay gated on explicit discovery (B1) and
        # links on the recursive/frontier need, exactly as before.
        post_fetch = await asyncio.to_thread(
            extract_post_fetch_discovery,
            html,
            response.final_url,
            js_base_url=normalized_url,
            max_links=limits.get("max_discovered_links", DEFAULT_MAX_DISCOVERED_LINKS),
            include_diagnostics=bool(discovery),
            include_links=status == "success" and _want_discovery_links,
            include_js_endpoints=bool(discovery),
            include_metadata=True,
            host_policy=host_policy,
        )
        if discovery:
            diagnostics.update(post_fetch["diagnostics"])
        retry_after = fetch_policy._retry_after_seconds(headers)
        if retry_after is not None:
            diagnostics["cooldown"] = {"retry_after_seconds": retry_after, "scope": "domain"}
        # Sitemap/feed discovery is only performed when discovery is requested
        # (the /v1/map path forces discovery, so maps are unaffected). Skipping
        # it for content-only crawls avoids the discovery fetches (B1).
        if discovery:
            diagnostics.update(
                await fetch_policy._discovery_diagnostics(
                    normalized_url=normalized_url,
                    engine_robots_sitemaps=robots_sitemaps,
                    robots_evaluated=robots_evaluated,
                    html=html,
                    policy=policy,
                    host_policy=host_policy,
                    # Base headers only: page-level If-None-Match /
                    # If-Modified-Since must never reach robots.txt or
                    # sitemap fetches (false 304s / missed sitemaps).
                    headers=base_headers,
                    http_fetcher=http_fetcher,
                )
            )
        if _max_depth > 0:
            # Site-wide discovery lists are frontier seeds, not per-page
            # payload: returning them on every page of a recursive crawl
            # wastes memory across thousands of pages (#13-fix).
            discovery_diag = diagnostics.get("discovery")
            if discovery_diag:
                discovery_diag.pop("sitemap_urls", None)
                discovery_diag.pop("feeds", None)
        # Emitted discovery URLs are attacker-influenced: only http(s) targets
        # may leave the service (no javascript:, file:, link-local oddities).
        # JS-endpoint extraction is discovery-oriented; skip it for
        # content-only crawls (B1).
        raw_endpoints = post_fetch["js_endpoints"]
        diagnostics["js_endpoints"] = filter_js_endpoints(
            raw_endpoints,
            resolve_against=response.final_url,
            policy=host_policy,
        )
        # Pagination detection stays on for all crawls: next_page_url is a
        # standard result field (returned even without discovery mode), so it is
        # always computed. Only the heavier discovery-only extras below are
        # gated on `discovery` (B1).
        next_page_url = post_fetch["next_page_url"]
        if next_page_url:
            diagnostics["next_page_url"] = next_page_url
        quality = body_decode._quality_score(
            status, classification, text, html, response.status_code, content_metrics=content_metrics
        )
        # Surface origin validators so callers can revalidate cheaply later.
        if strategy_decompression_failed or getattr(response, "decompression_failed", False):
            # Garbage compressed bytes must not masquerade as high-quality content.
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
            and not strategy_decompression_failed
            and diagnostics["content_metrics"]["visible_text_length"] >= 40
        ):
            # Boost only trustworthy content: garbage from failed decompression
            # can easily exceed the length threshold.
            quality = max(quality, 0.95)
        # discovered_links is consumed by frontier expansion and by discovery
        # consumers; content-only crawls (no discovery, depth 0) skip the
        # extraction entirely and return empty links (#2-fix).
        discovered_links = post_fetch["discovered_links"]
        discovered_link_count = post_fetch["discovered_link_count"]
        discovered_links_truncated = post_fetch["discovered_links_truncated"]
        attempt = CrawlStrategyAttempt(
            strategy=strategy,
            status=status,
            classification=classification,
            http_status=response.status_code,
            content_type=content_type,
            elapsed_ms=response.elapsed_ms,
            error=None if status == "success" else classification,
        )
        # T3/#2.1: release the large raw-HTML string before the result is built
        # when the caller did not request it (engine also strips at spill time).
        if not include_html:
            html = None
        return _make_engine_result(
            original_url=original_url,
            normalized_url=normalized_url,
            final_url=response.final_url,
            status=status,
            classification=classification,
            selected_strategy=strategy if status == "success" else None,
            attempt=attempt,
            http_status=response.status_code,
            headers=headers,
            content_type=content_type,
            html=html,
            text=text,
            llm_metadata=post_fetch["llm_metadata"] or None,
            markdown=markdown,
            extraction_method=extraction_method,
            quality_score=quality,
            classification_evidence=classification_evidence,
            diagnostics=diagnostics,
            discovered_link_count=discovered_link_count,
            discovered_links_truncated=discovered_links_truncated,
            discovered_links=discovered_links,
            error=None if status == "success" else classification,
            next_page_url=next_page_url,
            validators=origin_validators,
        )
    except Exception as error:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        # Single chokepoint for "a strategy raised": classify, log with the
        # stable code (raw detail server-side only), and build the redacted
        # terminal result. This used to hand-build the result here, which
        # drifted to classification="network_error" while error_code held the
        # real code.
        return _engine_result_from_exception(
            error=error,
            original_url=original_url,
            normalized_url=normalized_url,
            strategy=strategy,
            elapsed_ms=elapsed_ms,
            extra_diagnostics={
                "retry": retry_diagnostics,
                "runtime": {"elapsed_ms": elapsed_ms},
                # Mirror the SUCCESS path's redaction contract exactly: only
                # user_agent_family + cookie_jar leave the service. The raw
                # profile can carry live cookies (the dict
                # _cookie_header_for_url reads), fingerprint_seed and
                # cookie_namespace, and diagnostics reach the API response,
                # result store, persisted job.results and webhook payloads
                # (H4).
                "request_profile": {
                    "user_agent_family": str(
                        policy.get("request_profile", {}).get("user_agent_family", "generic_crawler")
                    ),
                    "cookie_jar": str(policy.get("request_profile", {}).get("cookie_jar", "none")),
                },
            },
        )
