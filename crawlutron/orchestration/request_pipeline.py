"""Crawl request pipeline phases."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, cast

from crawlutron.admission.admission import normalize_sitemap_candidates
from crawlutron.admission.admission_links import gather_robots_decisions
from crawlutron.admission.policy import DiscoveryFlags, discovery_flags
from crawlutron.admission.render_options import policy_wants_html
from crawlutron.config.settings import CrawlerSettings
from crawlutron.models.crawl_extraction_method import CrawlExtractionMethod
from crawlutron.models.crawl_policy import POLICY_SCHEMA_VERSION, CrawlPolicy
from crawlutron.models.crawl_request import CrawlRequest
from crawlutron.models.crawl_result import EngineCrawlResult
from crawlutron.models.crawl_task import CrawlTask
from crawlutron.models.crawl_worker import CrawlWorker
from crawlutron.models.host_policy import HostPolicy
from crawlutron.models.normalized_url import admit_or_none
from crawlutron.orchestration.recursive import run_recursive_crawl
from crawlutron.orchestration.strategy_chain import execute_crawl_chain
from crawlutron.runtime.pressure_gates import ResourceGate
from crawlutron.runtime.pressure_memory import read_memory_stats
from crawlutron.runtime.pressure_sizing import (
    TaskRamBudget,
    WeightedAdmission,
    build_admission_policy,
    derive_browser_pool_size,
    doc_worker_count,
    max_http_concurrency_for,
)
from crawlutron.safety.safe_regex import BoundedPattern
from crawlutron.storage.ids import new_request_id
from crawlutron.storage.result_store import CrawlSpillStore
from crawlutron.transport import robots as robots_policy
from crawlutron.transport.cache import HttpCache
from crawlutron.transport.content_cache import ContentCache
from crawlutron.transport.fetch_helpers import fetch_sitemap_urls
from crawlutron.transport.results import (
    TerminalResult,
    engine_result_from_exception,
    skipped_engine_result,
)
from crawlutron.transport.robots import FetchAndDecode, RobotsRules
from crawlutron.transport.scheduler import DomainScheduler
from crawlutron.vocabulary.strategies import BROWSER_STRATEGIES

if TYPE_CHECKING:
    from crawlutron.models.http_fetcher import HttpFetcher
    from crawlutron.models.normalized_url import NormalizedUrl
    from crawlutron.models.result_record import ProducerEnvelope
    from crawlutron.models.webhook_target import WebhookTarget
    from crawlutron.orchestration.engine import SeedCache


@dataclass(frozen=True)
class PipelineDependencies:
    get_content_cache: Callable[[], ContentCache]
    compile_scope_regex_safe: Callable[[str | None], BoundedPattern | None]
    stale_seeds: Callable[
        [Iterable[tuple[NormalizedUrl, str | None]], SeedCache | None, CrawlPolicy],
        set[NormalizedUrl],
    ]
    fetch_and_decode: FetchAndDecode | None
    shared_http_cache: HttpCache
    resource_gate: ResourceGate


@dataclass
class PipelineState:
    payload: CrawlRequest
    settings: CrawlerSettings
    url_pairs: list[tuple[str, NormalizedUrl]]
    policy: CrawlPolicy
    fetcher: HttpFetcher
    cffi: HttpFetcher
    host_policy: HostPolicy
    shutdown_event: asyncio.Event
    browser_worker: CrawlWorker | None
    cache: HttpCache | None
    content_cache: ContentCache | None
    webhook_url: WebhookTarget | None
    spill_store: CrawlSpillStore
    dependencies: PipelineDependencies
    jobs: list[CrawlTask] = field(default_factory=list)
    scheduler: DomainScheduler | None = None
    validators_by_url: dict[NormalizedUrl, dict[str, str]] = field(default_factory=dict)
    max_depth: int = 0
    max_pages: int = 0
    respect_robots: bool = False
    include_html: bool = True
    want_document_bodies: bool = False
    scope_pattern: BoundedPattern | None = None
    discovery: DiscoveryFlags = DiscoveryFlags(False, False, False)
    request_id: str = ""
    spill: CrawlSpillStore | None = None


def build_jobs(state: PipelineState) -> list[CrawlTask]:
    """One task per admitted pair; the pair's NormalizedUrl is reused, never
    rebuilt -- the gate's construction is the only one on the entry path.
    """
    return [
        CrawlTask(index=index, original_url=original_url, normalized_url=normalized_url)
        for index, (original_url, normalized_url) in enumerate(state.url_pairs)
    ]


def compute_ram_derived_concurrency(state: PipelineState) -> int:
    """The RAM-derived HTTP concurrency ceiling this process may sustain, the
    value weighted admission and the scheduler's connection ceiling both honor.
    """
    limit = read_memory_stats().total_bytes or 0
    # Reserve the doc pool too, exactly as build_admission_policy does, so
    # the scheduler ceiling and admission's max_http_concurrency cannot diverge.
    return max(1, max_http_concurrency_for(limit, derive_browser_pool_size(), doc_workers=doc_worker_count()))


def _build_scheduler(state: PipelineState) -> DomainScheduler:
    """Delegates to the single construction path; this layer only supplies the
    RAM-derived connection ceiling and the policy's RPS cap.
    """
    # The scheduler's own connection ceiling is the RAM-derived admission
    # ceiling, not a hardcoded constant: the two cannot diverge.
    return DomainScheduler.from_settings(
        state.settings,
        global_limit=compute_ram_derived_concurrency(state),
        max_rps=state.policy.limits.max_rps,
    )


def _build_validators(state: PipelineState) -> dict[NormalizedUrl, dict[str, str]]:
    validators_by_url: dict[NormalizedUrl, dict[str, str]] = {}
    for url, values in state.payload.validators.items():
        admitted = admit_or_none(url, policy=state.host_policy)
        if admitted is None:
            continue
        entry: dict[str, str] = {}
        if values.if_none_match:
            entry["if_none_match"] = values.if_none_match
        if values.if_modified_since:
            entry["if_modified_since"] = values.if_modified_since
        if not entry:
            continue
        validators_by_url[admitted] = entry
    return validators_by_url


def _validators_for(state: PipelineState, job: CrawlTask) -> dict[str, str] | None:
    return state.validators_by_url.get(job.normalized_url)


async def _run_job(state: PipelineState, job: CrawlTask) -> tuple[int, EngineCrawlResult]:
    robots_sitemaps = (
        []
        if not state.respect_robots
        else robots_policy.cached_sitemaps(
            job.normalized_url,
            policy=state.host_policy,
        )
    )
    budget = state.policy.limits.total_url_budget_seconds
    try:
        result = await asyncio.wait_for(
            execute_crawl_chain(
                original_url=job.original_url,
                normalized_url=job.normalized_url,
                policy=state.policy,
                http_fetcher=state.fetcher,
                cffi_fetcher=state.cffi,
                browser_worker=state.browser_worker,
                global_concurrency=state.settings.global_concurrency,
                cache=state.cache or state.dependencies.shared_http_cache,
                content_cache=state.content_cache
                or (state.dependencies.get_content_cache() if state.settings.content_cache_enabled else None),
                scheduler=state.scheduler,
                domain=job.domain,
                robots_sitemaps=robots_sitemaps,
                host_policy=state.host_policy,
                caller_validators=_validators_for(state, job),
                settings=state.settings,
            ),
            timeout=budget,
        )
    except TimeoutError as error:
        result = engine_result_from_exception(
            error=error,
            original_url=job.original_url,
            normalized_url=job.normalized_url,
            strategy=None,
            known_classification="url_budget_exceeded",
            extra_diagnostics={"url_budget_seconds": budget},
        )
    return job.index, result


async def _load_robots(state: PipelineState, target_url: NormalizedUrl) -> RobotsRules:
    return await robots_policy.load_robots_rules(
        target_url,
        state.fetcher,
        state.policy.limits.timeout_seconds,
        fetch_and_decode=state.dependencies.fetch_and_decode,
        policy=state.host_policy,
    )


async def _robots_allows(state: PipelineState, target_url: NormalizedUrl) -> bool:
    loaded = await _load_robots(state, target_url)
    return robots_policy.robots_allows(target_url, loaded.rules)


async def _robots_delay(state: PipelineState, target_url: NormalizedUrl) -> float | None:
    loaded = await _load_robots(state, target_url)
    return loaded.crawl_delay


async def _robots_disallowed(state: PipelineState, job: CrawlTask) -> EngineCrawlResult:
    """The result for a URL the robots policy refused to admit.

    The loaded policy says WHY: a forbidding policy, a refused one and an
    unreachable one all deny, and the caller is told which.
    """
    loaded = await _load_robots(state, job.normalized_url)
    return skipped_engine_result(
        TerminalResult(
            original_url=job.original_url,
            normalized_url=job.normalized_url,
            strategy=None,
            classification="robots_disallowed",
            error=loaded.source.error_code,
            diagnostics={
                "robots": {
                    "allowed": False,
                    "sitemaps": [],
                    "evaluation": loaded.source.value,
                }
            },
        )
    )


async def _seeds_for_job(state: PipelineState, job: CrawlTask) -> list[NormalizedUrl]:
    sitemap_url = admit_or_none(f"{job.normalized_url.origin}/sitemap.xml", policy=state.host_policy)
    if sitemap_url is None:
        return []
    found = await fetch_sitemap_urls(
        state.fetcher,
        sitemap_url,
        state.policy.limits.timeout_seconds,
        host_policy=state.host_policy,
    )
    candidates = normalize_sitemap_candidates(
        found,
        scope_pattern=state.scope_pattern,
    )
    stale = (
        await asyncio.to_thread(
            state.dependencies.stale_seeds,
            candidates,
            state.content_cache,
            state.policy,
        )
        if candidates
        else set()
    )
    return [url for url, _lastmod in candidates if url not in stale]


async def _sitemap_seed_urls(state: PipelineState) -> list[NormalizedUrl]:
    per_job = await asyncio.gather(*[_seeds_for_job(state, job) for job in state.jobs])
    return [seed for seeds in per_job for seed in seeds]


async def _worker(state: PipelineState, job: CrawlTask) -> EngineCrawlResult:
    _index, result = await _run_job(state, job)
    return result


async def _spill_one(state: PipelineState, index: int, result: EngineCrawlResult) -> None:
    """One page result to the crawl's spill rows; the store is bound before dispatch."""
    if state.spill is None:
        raise TypeError("spill must be bound before dispatch writes results")
    if not state.include_html:
        result = replace(result, html=None, text=None)
    await state.spill.aput(state.request_id, index, result)


def _build_admission(state: PipelineState) -> WeightedAdmission:
    """The RAM-weighted admission gate for one crawl, shared by both the
    recursive and non-recursive paths so they cannot drift on the same bound.
    """
    return WeightedAdmission(
        state.dependencies.resource_gate,
        build_admission_policy(
            read_memory_stats().total_bytes or 0,
            pool_size=derive_browser_pool_size(),
            max_concurrent_pages=state.settings.max_concurrent_pages,
            budget=TaskRamBudget(doc_workers=doc_worker_count()),
        ),
    )


def _bucket_tasks_by_tier(
    scheduler: DomainScheduler, tasks: list[CrawlTask], policy: CrawlPolicy
) -> tuple[list[CrawlTask], list[CrawlTask]]:
    """Split one task list into HTTP and browser tiers. A pinned browser strategy
    wins; an unpinned task follows its policy chain (a browser link in the chain
    may render, so it counts against browser capacity).
    """
    http_jobs: list[CrawlTask] = []
    browser_jobs: list[CrawlTask] = []
    chain_has_browser = any(strategy in BROWSER_STRATEGIES for strategy in policy.strategies)
    for task in tasks:
        pinned = scheduler.get_pinned_strategy(task.domain)
        if pinned in BROWSER_STRATEGIES or (pinned is None and chain_has_browser):
            browser_jobs.append(task)
        else:
            http_jobs.append(task)
    return http_jobs, browser_jobs


async def _dispatch_paced(
    state: PipelineState,
    scheduler: DomainScheduler,
    jobs: list[CrawlTask],
) -> None:
    """Dispatch a sync batch in admission-sized chunks, tier-aware: browser jobs
    are admitted against the browser capacity instead of being lumped into HTTP.
    """
    admission = _build_admission(state)
    remaining = list(jobs)
    floor = admission.saturated_dispatch_floor()
    while remaining:
        http_jobs, browser_jobs = _bucket_tasks_by_tier(scheduler, remaining, state.policy)
        admit_http, admit_browser = await admission.admit(len(http_jobs), len(browser_jobs))
        take_http = http_jobs[:admit_http] if admit_http else []
        take_browser = browser_jobs[:admit_browser] if admit_browser else []
        # Saturated gate: bounded progress floor -- dispatch k jobs (a fraction of
        # the HTTP ramp) so a pinned gate cannot leave the batch in flight forever
        # while the pass cost stays a fraction of the batch, not one cycle per job.
        if not take_http and not take_browser and remaining:
            if http_jobs:
                take_http = http_jobs[:floor]
            elif browser_jobs:
                take_browser = browser_jobs[:1]
        chunk = take_http + take_browser
        remaining = http_jobs[len(take_http) :] + browser_jobs[len(take_browser) :]
        await scheduler.run(
            chunk,
            lambda job: _worker(state, job),
            on_result=lambda index, result: _spill_one(state, index, result),
            failed_result=lambda job, exc: engine_result_from_exception(
                error=exc,
                original_url=job.original_url,
                normalized_url=job.normalized_url,
                strategy=None,
            ),
        )


async def _run_nonrecursive(state: PipelineState) -> None:
    scheduler = cast("DomainScheduler", state.scheduler)
    decisions = (
        await gather_robots_decisions(
            [job.normalized_url for job in state.jobs],
            lambda url: _robots_allows(state, url),
            lambda url: _robots_delay(state, url),
            timeout_seconds=state.policy.limits.timeout_seconds,
        )
        if state.respect_robots
        else [(True, None)] * len(state.jobs)
    )
    allowed_jobs: list[CrawlTask] = []
    for job, (allowed, crawl_delay) in zip(state.jobs, decisions, strict=True):
        if state.respect_robots and not allowed:
            await _spill_one(state, job.index, await _robots_disallowed(state, job))
            continue
        if state.respect_robots and crawl_delay and crawl_delay > 0:
            scheduler.set_domain_min_delay(job.domain, crawl_delay)
        allowed_jobs.append(job)
    if allowed_jobs:
        await _dispatch_paced(state, scheduler, allowed_jobs)


async def _run_recursive(state: PipelineState) -> None:
    scheduler = cast("DomainScheduler", state.scheduler)
    admission = _build_admission(state)
    await run_recursive_crawl(
        jobs=state.jobs,
        scheduler=scheduler,
        worker=lambda job: _worker(state, job),
        robots_allows=lambda url: _robots_allows(state, url),
        robots_crawl_delay=lambda url: _robots_delay(state, url),
        robots_disallowed=lambda job: _robots_disallowed(state, job),
        sitemap_seeds=lambda: _sitemap_seed_urls(state),
        spill=state.spill,
        request_id=state.request_id,
        shutdown_event=state.shutdown_event,
        max_depth=state.max_depth,
        max_pages=state.max_pages,
        respect_robots=state.respect_robots,
        include_html=state.include_html,
        want_document_bodies=state.want_document_bodies,
        scope_pattern=state.scope_pattern,
        global_concurrency=state.settings.global_concurrency,
        seed_sitemaps=state.discovery.sitemaps,
        resource_gate=state.dependencies.resource_gate,
        admission=admission,
        host_policy=state.host_policy,
        robots_timeout_seconds=state.policy.limits.timeout_seconds,
    )


async def _response(state: PipelineState) -> ProducerEnvelope:
    if state.spill is None:
        raise TypeError("spill must be set before the response is assembled")
    response: ProducerEnvelope = {
        "request_id": state.request_id,
        "submitted_count": len(state.url_pairs),
        "policy_schema_version": POLICY_SCHEMA_VERSION,
        "results": await state.spill.aload_ordered(state.request_id, policy=state.host_policy),
    }
    if state.webhook_url is not None:
        # The wire form is the redacted one: the raw URL is a delivery secret.
        response["webhook_url"] = state.webhook_url.redacted()
    return response


async def _run(state: PipelineState) -> ProducerEnvelope:
    state.jobs = build_jobs(state)
    state.scheduler = _build_scheduler(state)
    state.validators_by_url = _build_validators(state)
    limits = state.policy.limits
    state.max_depth = limits.max_depth
    state.max_pages = limits.max_pages
    state.respect_robots = limits.respect_robots
    state.include_html = policy_wants_html(state.policy)
    state.discovery = discovery_flags(state.policy)
    state.scope_pattern = state.dependencies.compile_scope_regex_safe(state.policy.scope_regex)
    state.want_document_bodies = state.policy.extraction is CrawlExtractionMethod.DOCUMENT
    state.request_id = new_request_id()
    state.spill = state.spill_store
    if state.max_depth > 0:
        await _run_recursive(state)
    else:
        await _run_nonrecursive(state)
    return await _response(state)


async def run_crawl_request(state: PipelineState) -> ProducerEnvelope:
    return await _run(state)


__all__ = ["run_crawl_request"]
