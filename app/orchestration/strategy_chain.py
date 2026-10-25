"""Strategy-chain execution for one crawl target."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, cast, get_args

from app.models.crawl_strategy import CrawlStrategy
from app.models.taxonomy import CrawlStrategyName
from app.orchestration.results import (
    TerminalResult,
    engine_result_from_exception,
    failed_engine_result,
)
from app.transport.browser_result import browser_strategy_result
from app.transport.http import http_strategy_result
from app.transport.strategy_request import StrategyRequest
from app.vocabulary.strategies import BROWSER_STRATEGIES, HTTP_CFFI_IMPERSONATE, HTTP_STRATEGIES

if TYPE_CHECKING:
    from app.config.settings import CrawlerSettings
    from app.models.crawl_diagnostics import CrawlDiagnostics
    from app.models.crawl_policy import CrawlPolicy
    from app.models.crawl_result import EngineCrawlResult
    from app.models.crawl_strategy_attempt import CrawlStrategyAttempt
    from app.models.crawl_worker import CrawlWorker
    from app.models.host_policy import HostPolicy
    from app.models.http_fetcher import HttpFetcher
    from app.models.normalized_url import NormalizedUrl
    from app.models.taxonomy import Classification, CrawlError
    from app.orchestration.scheduler import DomainScheduler

from app.vocabulary.taxonomy import (
    BROWSER_BLOCKED_CLASSIFICATIONS,
    HTTP_BLOCKED_CLASSIFICATIONS,
    CrawlClassification,
)

BLOCKED_CLASSIFICATIONS = HTTP_BLOCKED_CLASSIFICATIONS | BROWSER_BLOCKED_CLASSIFICATIONS

# Failures that mean the pinned strategy is no longer viable, so the chain must
# unpin and fall back to the base ladder instead of looping on a dead pin (#303).
PIN_INVALIDATING_CLASSIFICATIONS = frozenset(
    (
        CrawlClassification.browser_pool_exhausted.value,
        CrawlClassification.browser_crash.value,
    )
)


@dataclass(frozen=True)
class _ChainContext:
    original_url: str
    normalized_url: NormalizedUrl
    policy: CrawlPolicy
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
    settings: CrawlerSettings | None = None

    def request_for(self, strategy: CrawlStrategyName) -> StrategyRequest:
        """The page's identity for one ladder step.
        The chain owns this mapping because it knows which URLs and policy a run was
        dispatched with; a strategy building its own is one more place to mix pages up.
        """
        return StrategyRequest(
            original_url=self.original_url,
            normalized_url=self.normalized_url,
            strategy=strategy,
            policy=self.policy,
            host_policy=self.host_policy,
        )


@dataclass
class _ChainState:
    attempts: list[CrawlStrategyAttempt]
    last_result: EngineCrawlResult | None = None
    cffi_was_blocked: bool = False
    cffi_retry_done: bool = False


def _assert_strategy_names_agree() -> None:
    """The enum and the alias are two declarations of one set of names.
    They must agree because the ladder narrows one into the other, and nothing
    would notice a fifth member without a matching alias (it would type-check, then hit an unhandled step).
    """
    enum_names = {member.value for member in CrawlStrategy}
    alias_names = set(get_args(CrawlStrategyName))
    if enum_names != alias_names:
        raise RuntimeError(f"strategy name drift: enum {sorted(enum_names)} != alias {sorted(alias_names)}")


_assert_strategy_names_agree()


def starting_strategies(
    strategies: Iterable[CrawlStrategy],
    scheduler: DomainScheduler | None,
    domain: str,
) -> list[CrawlStrategyName]:
    """The escalation ladder from the domain's pin onward, or the whole ladder.
    Takes the three inputs the slice reads (not the whole context), so a pin test
    does not fabricate unrelated fields to reach this line -- or re-derive it locally.
    """
    # A static Literal cannot be derived from a runtime enum, so this narrowing is
    # asserted -- and _assert_strategy_names_agree() is what makes asserting it honest.
    ladder: list[CrawlStrategyName] = []
    for member in strategies:
        try:
            ladder.append(cast("CrawlStrategyName", CrawlStrategy(member).value))
        except ValueError as exc:
            raise ValueError(
                f"invalid_policy_strategies: .strategy={member!r} is not a CrawlStrategy"
            ) from exc
    if scheduler is None:
        return ladder
    pinned = scheduler.get_pinned_strategy(domain)
    if pinned is not None and pinned in ladder:
        return ladder[ladder.index(pinned) :]
    return ladder


@dataclass
class _ChainRun:
    """One walk of the ladder: the target's context plus this run's own state.
    They used to be threaded as a pair, re-spelled at every call site with no
    signature tying them together; now one value, and the steps are its methods.
    """

    context: _ChainContext
    state: _ChainState

    def terminal_failure(
        self,
        strategy: CrawlStrategyName,
        *,
        classification: Classification,
        error: CrawlError,
        diagnostics: CrawlDiagnostics | None = None,
    ) -> EngineCrawlResult:
        """A classified failed result for a step that never reached a strategy.
        The two pre-chain refusals (no renderer; strategy outside the ladder) are the
        same result with a different reason, built from the declared TerminalResult.
        """
        return failed_engine_result(
            TerminalResult(
                original_url=self.context.original_url,
                normalized_url=self.context.normalized_url,
                strategy=strategy,
                classification=classification,
                error=error,
                diagnostics=diagnostics,
            )
        )

    def with_policy(self, policy: CrawlPolicy) -> _ChainRun:
        """This same run re-pointed at another policy, carrying the same state.
        The cffi cookie retry is a ladder step, so it continues this run rather than
        starting fresh -- a new state would report only its own attempt.
        """
        return _ChainRun(replace(self.context, policy=policy), self.state)

    async def run_http(self, strategy: CrawlStrategyName) -> EngineCrawlResult:
        context = self.context
        fetcher = context.cffi_fetcher if strategy == HTTP_CFFI_IMPERSONATE else context.http_fetcher
        return await http_strategy_result(
            request=context.request_for(strategy),
            http_fetcher=fetcher,
            cache=context.cache,
            content_cache=context.content_cache,
            caller_validators=context.caller_validators,
            robots_sitemaps=context.robots_sitemaps,
            global_concurrency=context.global_concurrency,
            settings=context.settings,
        )

    async def run_browser(self, strategy: CrawlStrategyName) -> EngineCrawlResult:
        context = self.context
        if context.browser_worker is None:
            return self.terminal_failure(
                strategy,
                classification="browser_crash",
                error="browser_worker_unavailable",
                diagnostics={
                    "browser_profile": context.policy.browser_profile,
                    "runtime": {"error": "browser_worker_unavailable"},
                },
            )
        return await browser_strategy_result(
            request=context.request_for(strategy),
            browser_worker=context.browser_worker,
            settings=context.settings,
        )

    def record(self, strategy: CrawlStrategyName, result: EngineCrawlResult) -> None:
        state = self.state
        state.attempts.extend(result.attempted_strategies)
        state.last_result = result
        if strategy == HTTP_CFFI_IMPERSONATE and result.status != "success":
            state.cffi_was_blocked = True
        if result.classification != "rate_limited" or self.context.scheduler is None:
            return
        cooldown = result.diagnostics.get("cooldown") or {}
        retry_after = cooldown.get("retry_after_seconds")
        if retry_after is not None:
            self.context.scheduler.set_domain_cooldown(self.context.domain, float(retry_after))

    async def retry_cookies(
        self, strategy: CrawlStrategyName, result: EngineCrawlResult
    ) -> EngineCrawlResult | None:
        """Re-run the HTTP step carrying cookies the failed browser step harvested;
        returns the retry's result when it produced usable content, else None.
        Routed through execute, so the retry is enveloped like any other ladder step.
        """
        state = self.state
        if not (
            result.status == "success"
            and result.classification == "rendered"
            and strategy in BROWSER_STRATEGIES
            and state.cffi_was_blocked
            and not state.cffi_retry_done
        ):
            return None
        state.cffi_retry_done = True
        from app.transport.browser import get_browser_cookies_for
        from app.transport.request_profile import cookie_namespace_for

        # The crawl's own namespace, from the policy ``crawl_request`` bound
        # it from (#437): another caller's session never rides this retry.
        namespace = cookie_namespace_for(self.context.policy.request_profile)
        cookie_header = get_browser_cookies_for(self.context.normalized_url.site, namespace=namespace)
        if not cookie_header:
            return None
            # headers is dict[str, str] built from validated values plus this cookie, so there
            # is nothing here for validation to catch.
        retry_headers = {**self.context.policy.headers, "Cookie": cookie_header}
        retry_policy = self.context.policy.model_copy(update={"headers": retry_headers})
        retry_result = await self.with_policy(retry_policy).execute(HTTP_CFFI_IMPERSONATE)
        state.attempts.extend(retry_result.attempted_strategies)
        if retry_result.status == "success" and retry_result.quality_score > 0:
            return replace(retry_result, attempted_strategies=state.attempts)
        state.last_result = result
        return None

    async def execute(self, strategy: CrawlStrategyName) -> EngineCrawlResult:
        """Invoke ONE strategy step, converting any raise into a failed result.
        A strategy is a STEP in a ladder, not a whole URL: an adapter-boundary raise
        used to escape and skip the remaining rungs; both runs and the cookie retry route through here.
        """
        context = self.context
        try:
            if strategy in HTTP_STRATEGIES:
                return await self.run_http(strategy)
            if strategy in BROWSER_STRATEGIES:
                return await self.run_browser(strategy)
        except Exception as error:
            return engine_result_from_exception(
                error=error,
                original_url=context.original_url,
                normalized_url=context.normalized_url,
                strategy=strategy,
            )
        return self.terminal_failure(
            strategy,
            classification="unknown_error",
            error="unsupported_strategy",
        )


def _is_usable(result: EngineCrawlResult) -> bool:
    return result.status == "success" and (
        result.quality_score > 0 or result.classification == "not_modified"
    )


async def execute_crawl_chain(
    *,
    original_url: str,
    normalized_url: NormalizedUrl,
    policy: CrawlPolicy,
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
    settings: CrawlerSettings | None = None,
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
        settings=settings,
    )
    base_ladder = starting_strategies(context.policy.strategies, None, context.domain)
    start = 0
    if context.scheduler is not None:
        pinned = context.scheduler.get_pinned_strategy(context.domain)
        if pinned in base_ladder:
            start = base_ladder.index(pinned)
    run = _ChainRun(context, _ChainState(attempts=[]))
    return await _walk_ladder(run, context, base_ladder, start)


async def _walk_ladder(
    run: _ChainRun,
    context: _ChainContext,
    base_ladder: list[CrawlStrategyName],
    start: int,
) -> EngineCrawlResult:
    """One escalation walk of the full ladder, starting at ``start``. A pin that
    truncates the ladder may be cleared on a capacity failure so the domain falls
    back below the pin instead of looping on a dead strategy (#303).
    """
    index = start
    fell_back = False
    while index < len(base_ladder):
        strategy = base_ladder[index]
        result = await run.execute(strategy)
        run.record(strategy, result)
        if result.classification in BLOCKED_CLASSIFICATIONS:
            index += 1
            continue
        retry_result = await run.retry_cookies(strategy, result)
        if retry_result is not None:
            return retry_result
        if _is_usable(result):
            if context.scheduler is not None:
                context.scheduler.set_pinned_strategy(context.domain, strategy)
            return replace(result, attempted_strategies=run.state.attempts)
        # A pin-invalidating failure means the pinned strategy is no longer
        # viable: unpin and re-climb from the base instead of a failure loop.
        if (
            start > 0
            and context.scheduler is not None
            and not fell_back
            and result.classification in PIN_INVALIDATING_CLASSIFICATIONS
        ):
            context.scheduler.clear_pinned_strategy(context.domain)
            fell_back = True
            index = 0
            continue
        index += 1
    last = cast("EngineCrawlResult", run.state.last_result)
    return replace(last, attempted_strategies=run.state.attempts)


__all__ = ["execute_crawl_chain"]
