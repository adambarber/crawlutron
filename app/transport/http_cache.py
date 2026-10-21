"""HTTP cache behavior: limits, revalidation and 304 answers."""

from __future__ import annotations

import contextlib
import time

from app.admission.render_options import policy_wants_html
from app.models.crawl_diagnostics import CrawlDiagnostics
from app.models.crawl_result import EngineCrawlResult
from app.models.crawl_strategy_attempt import CrawlStrategyAttempt
from app.models.result_factory import build_engine_result
from app.models.taxonomy import Classification
from app.transport import content_cache_key, fetch_policy, request_profile
from app.transport.cache_control import (
    expires_seconds,
    forbids_shared_store,
    max_age_seconds,
    requires_revalidation,
)
from app.transport.http_response import (
    FetchedResponse,
    HttpStrategyContext,
    rebuild_response,
    response_result_fields,
    serialize_response,
)
from app.vocabulary.taxonomy import status_for_classification


def resolve_http_limits(ctx: HttpStrategyContext) -> None:
    """Resolve request validators, limits, headers, and extraction settings."""
    validators = ctx.caller_validators or {}
    for source, header in (
        ("if_none_match", "If-None-Match"),
        ("if_modified_since", "If-Modified-Since"),
    ):
        value = validators.get(source)
        if value:
            ctx.caller_conditional[header] = value

    ctx.profile = ctx.policy.request_profile
    ctx.limits = ctx.policy.limits
    ctx.robots_evaluated = ctx.limits.respect_robots
    ctx.timeout_seconds = ctx.limits.timeout_seconds
    ctx.max_retries = ctx.limits.max_retries
    ctx.max_content_length = content_cache_key.effective_max_content_length(
        ctx.limits, ctx.global_concurrency
    )
    ctx.base_headers = request_profile.headers_for_strategy(
        ctx.strategy, ctx.policy, ctx.normalized_url, profile=ctx.profile
    )
    ctx.request_headers = dict(ctx.base_headers)
    if ctx.cache is not None:
        ctx.request_headers.update(ctx.cache.conditional_headers(ctx.normalized_url))
    ctx.request_headers.update(ctx.caller_conditional)
    ctx.extraction = ctx.policy.extraction
    ctx.include_html = policy_wants_html(ctx.policy)


async def revalidate_content_cache(ctx: HttpStrategyContext) -> None:
    """Serve or revalidate a disk content-cache entry when one is available."""
    if ctx.content_cache is None:
        return
    ckey = content_cache_key.build_content_cache_key(
        ctx.normalized_url,
        ctx.strategy,
        ctx.base_headers,
        ctx.extraction,
        ctx.policy.css_selector,
        ctx.policy.xpath,
    )
    ctx.ckey = ckey
    cached = await ctx.content_cache.aget(ckey)
    if cached is None:
        return

    must_revalidate = requires_revalidation(cached["headers"])
    validator = ctx.cache.conditional_headers(ctx.normalized_url) if ctx.cache is not None else {}
    if not validator and ctx.caller_conditional:
        validator = dict(ctx.caller_conditional)
    if must_revalidate and not validator:
        # no-cache/must-revalidate may be stored but never replayed stale: source a
        # validator from the stored body, else re-fetch below rather than serve it (#247).
        lowered = {str(k).lower(): v for k, v in cached["headers"].items()}
        if lowered.get("etag"):
            validator["If-None-Match"] = lowered["etag"]
        if lowered.get("last-modified"):
            validator["If-Modified-Since"] = lowered["last-modified"]
    if not validator:
        if must_revalidate:
            return  # no validator to revalidate with: never serve stale, re-fetch.
        ctx.response = rebuild_response(cached, ctx)
        ctx.content_hit = True
        ctx.retry_diagnostics = {"attempts": 0, "transient_errors": [], "content_cache": "hit"}
        return

    revalidation_headers = dict(ctx.base_headers)
    revalidation_headers.update(validator)
    revalidation_headers.update(ctx.caller_conditional)
    ctx.response, ctx.retry_diagnostics = await fetch_policy.fetch_with_retries(
        ctx.http_fetcher,
        ctx.normalized_url,
        revalidation_headers,
        ctx.timeout_seconds,
        ctx.max_retries,
        ctx.max_content_length,
    )
    if ctx.response.status_code == 304:
        ctx.response = rebuild_response(cached, ctx)
        ctx.retry_diagnostics = {**ctx.retry_diagnostics, "content_cache": "hit_revalidated"}
        ctx.revalidated_304 = True
    else:
        ctx.retry_diagnostics = {**ctx.retry_diagnostics, "content_cache": "revalidated_fresh"}


def not_modified_result(ctx: HttpStrategyContext, resp: FetchedResponse) -> EngineCrawlResult | None:
    """Build the terminal result for a negotiated HTTP 304 response."""
    echoed_validators = resp.validators(ctx.caller_conditional)
    sent_conditional = any(key in ctx.request_headers for key in ("If-None-Match", "If-Modified-Since"))
    # The content-cache revalidated path is handled in ``_construct_http_result``, which keeps the
    # cached body; here (a caller-validator 304 with no body) both sents must line up.
    if resp.status_code != 304 or ctx.revalidated_304 or not sent_conditional:
        return None

    elapsed_ms = int((time.monotonic() - ctx.started) * 1000)
    # The label is spelled once and the status is asked for, so the attempt and
    # the result cannot disagree about it the day the taxonomy moves a default.
    classification: Classification = "not_modified"
    status = status_for_classification(classification)
    attempt = CrawlStrategyAttempt(
        strategy=ctx.strategy,
        status=status,
        classification=classification,
        http_status=304,
        content_type=None,
        elapsed_ms=elapsed_ms,
        error=None,
    )
    diagnostics: CrawlDiagnostics = {"retry": ctx.retry_diagnostics}
    if ctx.cache is not None:
        diagnostics["cache"] = "not_modified"
    if ctx.caller_conditional:
        diagnostics["conditional"] = dict(ctx.caller_conditional)
    return build_engine_result(
        **response_result_fields(ctx, resp, attempt),
        selected_strategy=ctx.strategy,
        http_status=304,
        diagnostics=diagnostics,
        validators=echoed_validators,
    )


async def store_http_caches(ctx: HttpStrategyContext, resp: FetchedResponse) -> None:
    """Store origin validators and fresh content after a successful fetch."""
    if forbids_shared_store(resp.headers):
        # no-store bans every cache; private bans the shared one (RFC 9111 5.2.2.6)
        # -- a single-user body must never persist for another caller's replay.
        return
    origin_ttl = max_age_seconds(resp.headers)
    if (
        ctx.cache is not None
        and resp.status_code < 400
        and (resp.headers.get("etag") or resp.headers.get("last-modified"))
    ):
        ctx.cache.store(ctx.normalized_url, dict(resp.headers))
    if ctx.content_cache is None or ctx.ckey is None or ctx.content_hit:
        return
    if resp.status_code < 300:
        # The origin's freshness lifetime wins over the local default (RFC 9111
        # 5.2.2.1), and max-age beats a conflicting Expires (5.3).
        origin_ttl = max_age_seconds(resp.headers)
        if origin_ttl is None:
            origin_ttl = expires_seconds(resp.headers, now=time.time())
        ttl = int(min(ctx.content_cache.default_ttl, origin_ttl)) if origin_ttl is not None else None
        if ttl is not None and ttl <= 0:
            # Already stale at write time (max-age=0 or a past Expires): the
            # OFF convention (#394) stores nothing rather than inverting it.
            return
        with contextlib.suppress(Exception):
            await ctx.content_cache.aset(ctx.ckey, serialize_response(resp.raw), ttl=ttl)
