"""Crawl request pipeline phases."""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from typing import Any, cast

from app.crawl import robots as robots_policy
from app.crawl.admission import gather_robots_decisions, normalize_sitemap_candidates
from app.crawl.fetch_helpers import fetch_sitemap_urls
from app.crawl.ids import new_request_id
from app.crawl.policy import DiscoveryFlags, discovery_flags
from app.crawl.recursive import RECURSIVE_RAMP_MAX, _spill_write, run_recursive_crawl
from app.crawl.render_options import policy_wants_html
from app.crawl.result_helpers import TerminalResult, _skipped_engine_result, engine_result_from_exception
from app.crawl.result_record import ProducerEnvelope
from app.crawl.robots import FetchAndDecode
from app.crawl.strategy_chain import execute_crawl_chain
from app.memory_pressure import read_memory_stats
from app.models.crawl_extraction_method import CrawlExtractionMethod
from app.models.crawl_policy import POLICY_SCHEMA_VERSION, CrawlPolicy
from app.models.crawl_task import CrawlTask
from app.models.crawl_worker import CrawlWorker
from app.models.engine_crawl_result import EngineCrawlResult
from app.models.host_policy import HostPolicy
from app.models.normalized_url import NormalizedUrl, admit_or_none
from app.resource_pressure import (
    ResourceGate,
    TaskRamBudget,
    WeightedAdmission,
    build_admission_policy,
    derive_browser_pool_size,
    doc_worker_count,
)
from app.safe_regex import BoundedPattern
from app.scheduler import DomainScheduler
from app.settings import CrawlerSettings
from app.transport.cache import HttpCache
from app.transport.content_cache import ContentCache
from app.vocabulary.limits import ENGINE_BATCH_CONCURRENCY_MULTIPLIER


@dataclass(frozen=True)
class _PipelineDependencies:
    get_content_cache: Callable[[], ContentCache]
    make_spill: Callable[[str | None], Any]
    compile_scope_regex_safe: Callable[[str | None], BoundedPattern | None]
    stale_seeds: Callable[[Iterable[tuple[NormalizedUrl, str | None]], Any, CrawlPolicy], set[NormalizedUrl]]
    fetch_and_decode: FetchAndDecode | None
    shared_http_cache: HttpCache
    resource_gate: ResourceGate


@dataclass
class _PipelineState:
    payload: dict[str, Any]
    settings: CrawlerSettings
    urls: list[str]
    policy: CrawlPolicy
    fetcher: Any
    cffi: Any
    host_policy: HostPolicy
    shutdown_event: asyncio.Event
    browser_worker: CrawlWorker | None
    cache: HttpCache | None
    content_cache: ContentCache | None
    webhook_url: str | None
    spill_path: str | None
    dependencies: _PipelineDependencies
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
    spill: Any = None


def _build_jobs(state: _PipelineState) -> list[CrawlTask]:
    jobs: list[CrawlTask] = []
    for index, original_url in enumerate(state.urls):
        try:
            normalized_url = NormalizedUrl(original_url, policy=state.host_policy)
            domain = normalized_url.site
        except ValueError as error:
            raise ValueError(f"invalid_url: {error}") from error
        jobs.append(
            CrawlTask(
                index=index,
                original_url=original_url,
                normalized_url=normalized_url,
                domain=domain,
            )
        )
    return jobs


def _build_scheduler(state: _PipelineState) -> DomainScheduler:
    limits = state.policy.limits
    return DomainScheduler(
        global_limit=RECURSIVE_RAMP_MAX * ENGINE_BATCH_CONCURRENCY_MULTIPLIER,
        per_domain_limit=state.settings.per_domain_concurrency,
        delay_seconds=state.settings.per_domain_delay_ms / 1000,
        max_rps=limits.max_rps,
        adaptive=state.settings.adaptive_throttling,
        adaptive_min_delay=state.settings.adaptive_min_delay,
        adaptive_max_delay=state.settings.adaptive_max_delay,
        adaptive_latency_factor=state.settings.adaptive_latency_factor,
    )


def _build_validators(state: _PipelineState) -> dict[NormalizedUrl, dict[str, str]]:
    validators_by_url: dict[NormalizedUrl, dict[str, str]] = {}
    for url, values in (state.payload.get("validators") or {}).items():
        admitted = admit_or_none(url, policy=state.host_policy)
        if admitted is None:
            continue
        entry = {
            "if_none_match": values.get("if_none_match"),
            "if_modified_since": values.get("if_modified_since"),
        }
        entry = {key: value for key, value in entry.items() if value}
        if not entry:
            continue
        validators_by_url[admitted] = entry
    return validators_by_url


def _validators_for(state: _PipelineState, job: CrawlTask) -> dict[str, str] | None:
    return state.validators_by_url.get(job.normalized_url)


async def _run_job(state: _PipelineState, job: CrawlTask) -> tuple[int, EngineCrawlResult]:
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


async def _load_robots(state: _PipelineState, target_url: NormalizedUrl):
    return await robots_policy.load_robots_rules(
        target_url,
        state.fetcher,
        state.policy.limits.timeout_seconds,
        fetch_and_decode=state.dependencies.fetch_and_decode,
        policy=state.host_policy,
    )


async def _robots_allows(state: _PipelineState, target_url: NormalizedUrl) -> bool:
    rules, _sitemaps, _delay = await _load_robots(state, target_url)
    return robots_policy.robots_allows(target_url, rules)


async def _robots_delay(state: _PipelineState, target_url: NormalizedUrl) -> float | None:
    _rules, _sitemaps, delay = await _load_robots(state, target_url)
    return delay


def _robots_disallowed(state: _PipelineState, job: CrawlTask) -> EngineCrawlResult:
    return _skipped_engine_result(
        TerminalResult(
            original_url=job.original_url,
            normalized_url=job.normalized_url,
            strategy=None,
            classification="robots_disallowed",
            error="robots_txt_disallowed",
            diagnostics={"robots": {"allowed": False}},
        )
    )


async def _seeds_for_job(state: _PipelineState, job: CrawlTask) -> list[NormalizedUrl]:
    sitemap_url = admit_or_none(
        f"{job.normalized_url.origin}/sitemap.xml", policy=state.host_policy
    )
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


async def _sitemap_seed_urls(state: _PipelineState) -> list[NormalizedUrl]:
    per_job = await asyncio.gather(*[_seeds_for_job(state, job) for job in state.jobs])
    return [seed for seeds in per_job for seed in seeds]


async def _worker(state: _PipelineState, job: CrawlTask) -> EngineCrawlResult:
    _index, result = await _run_job(state, job)
    return result


async def _spill_one(state: _PipelineState, index: int, result: EngineCrawlResult) -> None:
    if not state.include_html:
        result = replace(result, html=None, text=None)
    await _spill_write(state.spill, index, result)


async def _run_nonrecursive(state: _PipelineState) -> None:
    scheduler = cast("DomainScheduler", state.scheduler)
    decisions = (
        await gather_robots_decisions(
            [job.normalized_url for job in state.jobs],
            lambda url: _robots_allows(state, url),
            lambda url: _robots_delay(state, url),
        )
        if state.respect_robots
        else [(True, None)] * len(state.jobs)
    )
    allowed_jobs: list[CrawlTask] = []
    for job, (allowed, crawl_delay) in zip(state.jobs, decisions, strict=True):
        if state.respect_robots and not allowed:
            await _spill_write(state.spill, job.index, _robots_disallowed(state, job))
            continue
        if state.respect_robots and crawl_delay and crawl_delay > 0:
            scheduler.set_domain_min_delay(job.domain, crawl_delay)
        allowed_jobs.append(job)
    if allowed_jobs:
        await scheduler.run(
            allowed_jobs,
            lambda job: _worker(state, job),
            on_result=lambda index, result: _spill_one(state, index, result),
            failed_result=lambda job, exc: engine_result_from_exception(
                error=exc,
                original_url=job.original_url,
                normalized_url=job.normalized_url,
                strategy=None,
            ),
        )


async def _run_recursive(state: _PipelineState) -> None:
    scheduler = cast("DomainScheduler", state.scheduler)
    admission = WeightedAdmission(
        state.dependencies.resource_gate,
        build_admission_policy(
            read_memory_stats().total_bytes or 0,
            pool_size=derive_browser_pool_size(),
            max_concurrent_pages=state.settings.max_concurrent_pages,
            budget=TaskRamBudget(doc_workers=doc_worker_count()),
        ),
    )
    await run_recursive_crawl(
        jobs=state.jobs,
        scheduler=scheduler,
        worker=lambda job: _worker(state, job),
        robots_allows=lambda url: _robots_allows(state, url),
        robots_crawl_delay=lambda url: _robots_delay(state, url),
        robots_disallowed=lambda job: _robots_disallowed(state, job),
        sitemap_seeds=lambda: _sitemap_seed_urls(state),
        spill=state.spill,
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
    )


async def _response(state: _PipelineState) -> ProducerEnvelope:
    response: ProducerEnvelope = {
        "request_id": new_request_id(),
        "submitted_count": len(state.urls),
        "policy_schema_version": POLICY_SCHEMA_VERSION,
        "results": await asyncio.to_thread(
            functools.partial(state.spill.load_ordered, policy=state.host_policy)
        ),
    }
    if state.webhook_url is not None:
        response["webhook_url"] = state.webhook_url
    return response


async def _run(state: _PipelineState) -> ProducerEnvelope:
    state.jobs = _build_jobs(state)
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
    state.spill = state.dependencies.make_spill(state.spill_path)
    if state.max_depth > 0:
        await _run_recursive(state)
    else:
        await _run_nonrecursive(state)
    response = await _response(state)
    state.spill.close()
    return response


async def run_crawl_request(state: _PipelineState) -> ProducerEnvelope:
    return await _run(state)


__all__ = ["run_crawl_request"]
