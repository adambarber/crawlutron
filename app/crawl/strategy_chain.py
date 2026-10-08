"""Strategy-chain execution for one crawl target."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Any, cast, get_args

from app.crawl.result_helpers import TerminalResult, _failed_engine_result, engine_result_from_exception
from app.models.crawl_policy import CrawlPolicy
from app.models.crawl_result import CrawlStrategyAttempt
from app.models.crawl_strategy import CrawlStrategy
from app.models.crawl_worker import CrawlWorker
from app.models.engine_crawl_result import EngineCrawlResult
from app.models.host_policy import HostPolicy
from app.models.http_fetcher import HttpFetcher
from app.models.normalized_url import NormalizedUrl
from app.models.taxonomy import Classification, CrawlError, CrawlStrategyName
from app.scheduler import DomainScheduler
from app.transport.browser_result import browser_strategy_result
from app.transport.http import http_strategy_result
from app.transport.strategy_request import StrategyRequest
from app.vocabulary.strategies import BROWSER_STRATEGIES, HTTP_CFFI_IMPERSONATE, HTTP_STRATEGIES
from app.vocabulary.taxonomy import BROWSER_BLOCKED_CLASSIFICATIONS, HTTP_BLOCKED_CLASSIFICATIONS

BLOCKED_CLASSIFICATIONS = HTTP_BLOCKED_CLASSIFICATIONS | BROWSER_BLOCKED_CLASSIFICATIONS


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

    def request_for(self, strategy: CrawlStrategyName) -> StrategyRequest:
        """The page's identity for one ladder step.

        The chain owns this mapping because the chain is what knows which URLs
        and policy a run was dispatched with; a strategy that built its own
        would be one more place to mix two pages up.
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

    They have to agree, because the ladder narrows one into the other, and
    nothing in either declaration would notice if a fifth enum member appeared
    without a matching alias entry: it would type-check, and then reach a
    strategy the chain does not handle. Cheap at import, so it is cheap here.
    """
    enum_names = {member.value for member in CrawlStrategy}
    alias_names = set(get_args(CrawlStrategyName))
    if enum_names != alias_names:
        raise RuntimeError(f"strategy name drift: enum {sorted(enum_names)} != alias {sorted(alias_names)}")


_assert_strategy_names_agree()


def _starting_strategies(
    strategies: Iterable[CrawlStrategy],
    scheduler: DomainScheduler | None,
    domain: str,
) -> list[CrawlStrategyName]:
    """The escalation ladder from the domain's pin onward, or the whole ladder.

    Takes the three inputs the slice reads rather than the whole
    ``_ChainContext``. A pin test that has to fabricate thirteen unrelated
    fields to reach this line is a test that will not be written here -- and the
    copy of the slice that gets written instead stops being what production
    does, which is how a pinning regression ships with pinning tests green.
    """
    # The policy holds the enum; the chain's currency is the name alias. Python
    # cannot derive one static Literal from a runtime enum, so this narrowing is
    # asserted rather than derived -- and _assert_strategy_names_agree() below
    # is what makes asserting it honest.
    #
    # The conversion is a defence, not a validation: ``CrawlStrategyName`` is a
    # Literal, so the read edge has already rejected a member that is not a
    # strategy, and only an unvalidated escape (``model_copy(update=...)``)
    # can put one here. When it does, the raise names the field and the member
    # rather than repeating pydantic's value-only complaint, so a caller
    # holding a dict-shaped policy learns WHICH field to fix -- the same
    # contract every other policy rejection in this module answers with.
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

    ``context`` and ``state`` were threaded through every step as a pair, so each
    helper opened with the same two parameters, the pair was re-spelled at every
    call site, and nothing in any signature said the two belonged together -- a
    state from another walk was a legal argument. They are the two halves of one
    run, so they are one value and the steps are its methods. That is also what
    lets :meth:`terminal_failure` read the target's identity off ``self`` instead
    of having each of its call sites declare it again.
    """

    context: _ChainContext
    state: _ChainState

    def terminal_failure(
        self,
        strategy: CrawlStrategyName,
        *,
        classification: Classification,
        error: CrawlError,
        diagnostics: dict[str, Any] | None = None,
    ) -> EngineCrawlResult:
        """A classified failed result for a step that never reached a strategy.

        The two pre-chain refusals -- no renderer available, and a strategy
        outside the ladder -- are the same result carrying a different reason.
        ``TerminalResult`` exists so the terminal field list is declared once;
        a call site that re-states ``original_url``/``normalized_url``/``strategy``
        is a second declaration of the identity, which is the duplication
        ``TerminalResult``'s own docstring says it was built to remove.
        """
        return _failed_engine_result(
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

        The cffi cookie retry is a ladder step like any other, so it continues
        this run's state rather than starting a new one -- a retry that began a
        fresh state would report only its own attempt and lose the ladder's.
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
        """Re-run the HTTP step carrying cookies the failed browser step harvested.

        Returns the retry's result when it produced usable content, else None.
        Routed through :meth:`execute` rather than :meth:`run_http`, because the
        retry is a ladder step like any other and so is enveloped like any other.
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

        cookie_header = get_browser_cookies_for(self.context.normalized_url.site)
        if not cookie_header:
            return None
        # model_copy, not a dict round-trip: the only thing changing is one
        # header, and headers is dict[str, str] built from validated values plus
        # this cookie, so there is nothing here for validation to catch.
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

        A strategy is a STEP in a ladder, not a whole URL. The HTTP strategy has
        its own broad-except sink and the browser worker catches inside
        ``render``, but a raise at the adapter boundary between them had nowhere
        to go: it escaped the chain, so it was enveloped with no strategy
        attribution and every remaining ladder step skipped -- one BROWSER_HEADLESS
        binding error ended a URL that still had BROWSER_HEADED_WARMED to try.
        Both ``run_*`` methods AND the cffi cookie retry route through here, so a
        raise has exactly one place it can become a result.
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
    run = _ChainRun(context, _ChainState(attempts=[]))
    for strategy in _starting_strategies(context.policy.strategies, context.scheduler, context.domain):
        result = await run.execute(strategy)
        run.record(strategy, result)
        if result.classification in BLOCKED_CLASSIFICATIONS:
            continue
        retry_result = await run.retry_cookies(strategy, result)
        if retry_result is not None:
            return retry_result
        if _is_usable(result):
            if context.scheduler is not None:
                context.scheduler.set_pinned_strategy(context.domain, strategy)
            return replace(result, attempted_strategies=run.state.attempts)
    last = cast("EngineCrawlResult", run.state.last_result)
    return replace(last, attempted_strategies=run.state.attempts)


__all__ = ["execute_crawl_chain"]
