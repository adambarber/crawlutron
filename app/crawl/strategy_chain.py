"""Strategy-chain execution for one crawl target."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from app.constants import (
    BROWSER_BLOCKED_CLASSIFICATIONS,
    BROWSER_STRATEGIES,
    HTTP_BLOCKED_CLASSIFICATIONS,
    HTTP_CFFI_IMPERSONATE,
    HTTP_STRATEGIES,
)
from app.crawl.result_helpers import _engine_result_from_exception, _failed_engine_result
from app.crawl.strategies.browser_result import browser_strategy_result
from app.crawl.strategies.http import _http_strategy_result
from app.models import (
    CrawlStrategyAttempt,
    CrawlStrategyName,
    CrawlWorker,
    EngineCrawlResult,
    HttpFetcher,
)
from app.scheduler import DomainScheduler
from app.validation import HostPolicy, registrable_domain

BLOCKED_CLASSIFICATIONS = HTTP_BLOCKED_CLASSIFICATIONS | BROWSER_BLOCKED_CLASSIFICATIONS


@dataclass(frozen=True)
class _ChainContext:
    original_url: str
    normalized_url: str
    policy: dict[str, Any]
    http_fetcher: HttpFetcher
    cffi_fetcher: HttpFetcher
    browser_worker: CrawlWorker | None
    cache: Any
    content_cache: Any
    scheduler: DomainScheduler | None
    domain: str
    caller_validators: dict[str, str] | None
    robots_sitemaps: list[str] | None
    host_policy: HostPolicy
    global_concurrency: int | None


@dataclass
class _ChainState:
    attempts: list[CrawlStrategyAttempt]
    last_result: EngineCrawlResult | None = None
    cffi_was_blocked: bool = False
    cffi_retry_done: bool = False


def _starting_strategies(context: _ChainContext) -> list[CrawlStrategyName]:
    strategies = list(context.policy["strategies"])
    if context.scheduler is None:
        return strategies
    pinned = context.scheduler.get_pinned_strategy(context.domain)
    if pinned is not None and pinned in strategies:
        return strategies[strategies.index(pinned) :]
    return strategies


async def _run_http_strategy(
    context: _ChainContext,
    strategy: CrawlStrategyName,
) -> EngineCrawlResult:
    fetcher = context.cffi_fetcher if strategy == HTTP_CFFI_IMPERSONATE else context.http_fetcher
    return await _http_strategy_result(
        original_url=context.original_url,
        normalized_url=context.normalized_url,
        strategy=strategy,
        policy=context.policy,
        http_fetcher=fetcher,
        cache=context.cache,
        content_cache=context.content_cache,
        caller_validators=context.caller_validators,
        robots_sitemaps=context.robots_sitemaps,
        host_policy=context.host_policy,
        global_concurrency=context.global_concurrency,
    )


async def _run_browser_strategy(
    context: _ChainContext,
    strategy: CrawlStrategyName,
) -> EngineCrawlResult:
    if context.browser_worker is None:
        return _failed_engine_result(
            original_url=context.original_url,
            normalized_url=context.normalized_url,
            strategy=strategy,
            classification="browser_crash",
            error="browser_worker_unavailable",
            diagnostics={
                "browser_profile": context.policy.get("browser_profile") or {},
                "runtime": {"error": "browser_worker_unavailable"},
            },
        )
    return await browser_strategy_result(
        original_url=context.original_url,
        normalized_url=context.normalized_url,
        strategy=strategy,
        policy=context.policy,
        browser_worker=context.browser_worker,
        host_policy=context.host_policy,
    )


def _record_result(
    context: _ChainContext,
    state: _ChainState,
    strategy: CrawlStrategyName,
    result: EngineCrawlResult,
) -> None:
    state.attempts.extend(result.attempted_strategies)
    state.last_result = result
    if strategy == HTTP_CFFI_IMPERSONATE and result.status != "success":
        state.cffi_was_blocked = True
    if result.classification != "rate_limited" or context.scheduler is None:
        return
    cooldown = result.diagnostics.get("cooldown") or {}
    retry_after = cooldown.get("retry_after_seconds")
    if retry_after is not None:
        context.scheduler.set_domain_cooldown(context.domain, float(retry_after))


async def _retry_browser_cookies(
    context: _ChainContext,
    state: _ChainState,
    strategy: CrawlStrategyName,
    result: EngineCrawlResult,
) -> EngineCrawlResult | None:
    if not (
        result.status == "success"
        and result.classification == "rendered"
        and strategy in BROWSER_STRATEGIES
        and state.cffi_was_blocked
        and not state.cffi_retry_done
    ):
        return None
    state.cffi_retry_done = True
    from app.crawl.strategies.browser import get_browser_cookies_for

    cookie_header = get_browser_cookies_for(registrable_domain(context.original_url))
    if not cookie_header:
        return None
    retry_policy = dict(context.policy)
    retry_headers = dict(retry_policy.get("headers") or {})
    retry_headers["Cookie"] = cookie_header
    retry_policy["headers"] = retry_headers
    # Through _execute_strategy, not _run_http_strategy: the retry is a ladder
    # step like any other, so its raise is enveloped like any other.
    retry_result = await _execute_strategy(replace(context, policy=retry_policy), HTTP_CFFI_IMPERSONATE)
    state.attempts.extend(retry_result.attempted_strategies)
    if retry_result.status == "success" and retry_result.quality_score > 0:
        return replace(retry_result, attempted_strategies=state.attempts)
    state.last_result = result
    return None


def _is_usable(result: EngineCrawlResult) -> bool:
    return result.status == "success" and (
        result.quality_score > 0 or result.classification == "not_modified"
    )


async def _execute_strategy(
    context: _ChainContext,
    strategy: CrawlStrategyName,
) -> EngineCrawlResult:
    """Invoke ONE strategy step, converting any raise into a failed result.

    A strategy is a STEP in a ladder, not a whole URL. The HTTP strategy has
    its own broad-except sink and the browser worker catches inside
    ``render``, but a raise at the adapter boundary between them had nowhere
    to go: it escaped the chain, so it was enveloped with no strategy
    attribution and every remaining ladder step skipped -- one BROWSER_HEADLESS
    binding error ended a URL that still had BROWSER_HEADED_WARMED to try.
    Both ``_run_*`` helpers AND the cffi cookie retry route through here, so a
    raise has exactly one place it can become a result.
    """
    try:
        if strategy in HTTP_STRATEGIES:
            return await _run_http_strategy(context, strategy)
        if strategy in BROWSER_STRATEGIES:
            return await _run_browser_strategy(context, strategy)
    except Exception as error:
        return _engine_result_from_exception(
            error=error,
            original_url=context.original_url,
            normalized_url=context.normalized_url,
            strategy=strategy,
        )
    return _failed_engine_result(
        original_url=context.original_url,
        normalized_url=context.normalized_url,
        strategy=strategy,
        classification="unknown_error",
        error="unsupported_strategy",
    )


async def execute_crawl_chain(
    *,
    original_url: str,
    normalized_url: str,
    policy: dict[str, Any],
    http_fetcher: HttpFetcher,
    cffi_fetcher: HttpFetcher,
    browser_worker: CrawlWorker | None,
    cache: Any = None,
    content_cache: Any = None,
    scheduler: DomainScheduler | None = None,
    domain: str = "",
    caller_validators: dict[str, str] | None = None,
    robots_sitemaps: list[str] | None = None,
    host_policy: HostPolicy,
    global_concurrency: int | None = None,
) -> EngineCrawlResult:
    context = _ChainContext(
        original_url=original_url,
        normalized_url=normalized_url,
        policy=policy,
        http_fetcher=http_fetcher,
        cffi_fetcher=cffi_fetcher,
        browser_worker=browser_worker,
        cache=cache,
        content_cache=content_cache,
        scheduler=scheduler,
        domain=domain,
        caller_validators=caller_validators,
        robots_sitemaps=robots_sitemaps,
        host_policy=host_policy,
        global_concurrency=global_concurrency,
    )
    state = _ChainState(attempts=[])
    for strategy in _starting_strategies(context):
        result = await _execute_strategy(context, strategy)
        _record_result(context, state, strategy, result)
        if result.classification in BLOCKED_CLASSIFICATIONS:
            continue
        retry_result = await _retry_browser_cookies(context, state, strategy, result)
        if retry_result is not None:
            return retry_result
        if _is_usable(result):
            if context.scheduler is not None:
                context.scheduler.set_pinned_strategy(context.domain, strategy)
            return replace(result, attempted_strategies=state.attempts)
    assert state.last_result is not None
    return replace(state.last_result, attempted_strategies=state.attempts)


__all__ = ["execute_crawl_chain"]
