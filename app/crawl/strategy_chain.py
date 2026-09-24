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
from app.crawl.result_helpers import _failed_engine_result
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


@dataclass
class _ChainState:
    attempts: list[CrawlStrategyAttempt]
    last_result: EngineCrawlResult | None = None
    cffi_was_blocked: bool = False
    cffi_retry_done: bool = False


def _starting_strategies(
    policy: dict[str, Any],
    scheduler: DomainScheduler | None,
    domain: str,
) -> list[CrawlStrategyName]:
    strategies = list(policy["strategies"])
    if scheduler is None:
        return strategies
    pinned = scheduler.get_pinned_strategy(domain)
    if pinned is not None and pinned in strategies:
        return strategies[strategies.index(pinned) :]
    return strategies


async def _run_http_strategy(
    strategy: CrawlStrategyName,
    *,
    original_url: str,
    normalized_url: str,
    policy: dict[str, Any],
    http_fetcher: HttpFetcher,
    cffi_fetcher: HttpFetcher,
    cache: Any,
    content_cache: Any,
    caller_validators: dict[str, str] | None,
    robots_sitemaps: list[str] | None,
    host_policy: HostPolicy,
    global_concurrency: int | None,
) -> EngineCrawlResult:
    fetcher = cffi_fetcher if strategy == HTTP_CFFI_IMPERSONATE else http_fetcher
    return await _http_strategy_result(
        original_url=original_url,
        normalized_url=normalized_url,
        strategy=strategy,
        policy=policy,
        http_fetcher=fetcher,
        cache=cache,
        content_cache=content_cache,
        caller_validators=caller_validators,
        robots_sitemaps=robots_sitemaps,
        host_policy=host_policy,
        global_concurrency=global_concurrency,
    )


async def _run_browser_strategy(
    strategy: CrawlStrategyName,
    *,
    original_url: str,
    normalized_url: str,
    policy: dict[str, Any],
    browser_worker: CrawlWorker | None,
    host_policy: HostPolicy,
) -> EngineCrawlResult:
    if browser_worker is None:
        return _failed_engine_result(
            original_url=original_url,
            normalized_url=normalized_url,
            strategy=strategy,
            classification="browser_crash",
            error="browser_worker_unavailable",
            diagnostics={
                "browser_profile": policy.get("browser_profile") or {},
                "runtime": {"error": "browser_worker_unavailable"},
            },
        )
    return await browser_strategy_result(
        original_url=original_url,
        normalized_url=normalized_url,
        strategy=strategy,
        policy=policy,
        browser_worker=browser_worker,
        host_policy=host_policy,
    )


def _record_result(
    state: _ChainState,
    strategy: CrawlStrategyName,
    result: EngineCrawlResult,
    scheduler: DomainScheduler | None,
    domain: str,
) -> None:
    state.attempts.extend(result.attempted_strategies)
    state.last_result = result
    if strategy == HTTP_CFFI_IMPERSONATE and result.status != "success":
        state.cffi_was_blocked = True
    if result.classification != "rate_limited" or scheduler is None:
        return
    cooldown = result.diagnostics.get("cooldown") or {}
    retry_after = cooldown.get("retry_after_seconds")
    if retry_after is not None:
        scheduler.set_domain_cooldown(domain, float(retry_after))


async def _retry_browser_cookies(
    *,
    strategy: CrawlStrategyName,
    result: EngineCrawlResult,
    state: _ChainState,
    original_url: str,
    normalized_url: str,
    policy: dict[str, Any],
    cffi_fetcher: HttpFetcher,
    cache: Any,
    content_cache: Any,
    caller_validators: dict[str, str] | None,
    robots_sitemaps: list[str] | None,
    host_policy: HostPolicy,
    global_concurrency: int | None,
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

    cookie_header = get_browser_cookies_for(registrable_domain(original_url))
    if not cookie_header:
        return None
    retry_policy = dict(policy)
    retry_headers = dict(retry_policy.get("headers") or {})
    retry_headers["Cookie"] = cookie_header
    retry_policy["headers"] = retry_headers
    retry_result = await _http_strategy_result(
        original_url=original_url,
        normalized_url=normalized_url,
        strategy=HTTP_CFFI_IMPERSONATE,
        policy=retry_policy,
        http_fetcher=cffi_fetcher,
        cache=cache,
        content_cache=content_cache,
        caller_validators=caller_validators,
        robots_sitemaps=robots_sitemaps,
        host_policy=host_policy,
        global_concurrency=global_concurrency,
    )
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
    strategy: CrawlStrategyName,
    *,
    original_url: str,
    normalized_url: str,
    policy: dict[str, Any],
    http_fetcher: HttpFetcher,
    cffi_fetcher: HttpFetcher,
    browser_worker: CrawlWorker | None,
    cache: Any,
    content_cache: Any,
    caller_validators: dict[str, str] | None,
    robots_sitemaps: list[str] | None,
    host_policy: HostPolicy,
    global_concurrency: int | None,
) -> EngineCrawlResult:
    if strategy in HTTP_STRATEGIES:
        return await _run_http_strategy(
            strategy,
            original_url=original_url,
            normalized_url=normalized_url,
            policy=policy,
            http_fetcher=http_fetcher,
            cffi_fetcher=cffi_fetcher,
            cache=cache,
            content_cache=content_cache,
            caller_validators=caller_validators,
            robots_sitemaps=robots_sitemaps,
            host_policy=host_policy,
            global_concurrency=global_concurrency,
        )
    if strategy in BROWSER_STRATEGIES:
        return await _run_browser_strategy(
            strategy,
            original_url=original_url,
            normalized_url=normalized_url,
            policy=policy,
            browser_worker=browser_worker,
            host_policy=host_policy,
        )
    return _failed_engine_result(
        original_url=original_url,
        normalized_url=normalized_url,
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
    state = _ChainState(attempts=[])
    for strategy in _starting_strategies(policy, scheduler, domain):
        result = await _execute_strategy(
            strategy,
            original_url=original_url,
            normalized_url=normalized_url,
            policy=policy,
            http_fetcher=http_fetcher,
            cffi_fetcher=cffi_fetcher,
            browser_worker=browser_worker,
            cache=cache,
            content_cache=content_cache,
            caller_validators=caller_validators,
            robots_sitemaps=robots_sitemaps,
            host_policy=host_policy,
            global_concurrency=global_concurrency,
        )
        _record_result(state, strategy, result, scheduler, domain)
        if result.classification in BLOCKED_CLASSIFICATIONS:
            continue
        retry_result = await _retry_browser_cookies(
            strategy=strategy,
            result=result,
            state=state,
            original_url=original_url,
            normalized_url=normalized_url,
            policy=policy,
            cffi_fetcher=cffi_fetcher,
            cache=cache,
            content_cache=content_cache,
            caller_validators=caller_validators,
            robots_sitemaps=robots_sitemaps,
            host_policy=host_policy,
            global_concurrency=global_concurrency,
        )
        if retry_result is not None:
            return retry_result
        if _is_usable(result):
            if scheduler is not None:
                scheduler.set_pinned_strategy(domain, strategy)
            return replace(result, attempted_strategies=state.attempts)
    assert state.last_result is not None
    return replace(state.last_result, attempted_strategies=state.attempts)


__all__ = ["execute_crawl_chain"]
