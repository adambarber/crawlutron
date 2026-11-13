"""The scheduler's RAM-derived HTTP ceiling matches admission's (#367)."""

import asyncio

from crawlutron.config.settings import CrawlerSettings
from crawlutron.models.crawl_policy import CrawlPolicy as ProbeCrawlPolicy
from crawlutron.models.crawl_request import CrawlRequest
from crawlutron.orchestration.request_pipeline import (
    PipelineDependencies,
    PipelineState,
    compute_ram_derived_concurrency,
)
from crawlutron.runtime.pressure_gates import ResourceGate
from crawlutron.runtime.pressure_memory import read_memory_stats
from crawlutron.runtime.pressure_sizing import (
    TaskRamBudget,
    build_admission_policy,
    doc_worker_count,
    max_http_concurrency_for,
)
from crawlutron.storage.result_store import CrawlSpillStore
from crawlutron.transport.browser_pool import derive_browser_pool_size
from crawlutron.transport.cache import HttpCache
from crawlutron.transport.content_cache import ContentCache
from tests._fetch_doubles import FetcherDouble
from tests._policy_factories import UNRESTRICTED_HOST_POLICY


def test_ram_derived_scheduler_ceiling_accounts_for_doc_workers():
    """The scheduler's RAM-derived ceiling reserves the doc pool too (#367):
    the same limit yields the same ceiling for the scheduler and for weighted
    admission, which passes doc_workers=doc_worker_count()."""
    limit = read_memory_stats().total_bytes or 0
    expected = max(
        1,
        max_http_concurrency_for(limit, derive_browser_pool_size(), doc_workers=doc_worker_count()),
    )
    # The pipeline passes a state whose values the ceiling does not read; the
    # test pins that independence with the same call shape, a minimal state.
    assert compute_ram_derived_concurrency(_ceiling_probe_state()) == expected


def test_ram_derived_ceiling_matches_admission_policy():
    """End to end (#367): the scheduler ceiling equals admission's max_http_concurrency."""
    limit = read_memory_stats().total_bytes or 0
    admission = build_admission_policy(
        limit,
        pool_size=derive_browser_pool_size(),
        max_concurrent_pages=3,
        budget=TaskRamBudget(doc_workers=doc_worker_count()),
    )
    assert compute_ram_derived_concurrency(_ceiling_probe_state()) == admission.max_http_concurrency


def _spill_store_never_used() -> CrawlSpillStore:
    """A store the ceiling never opens rows for; in-memory, closed by the test."""
    return CrawlSpillStore(":memory:")


def _never_read_dependencies() -> PipelineDependencies:
    """A dependencies bag the ceiling never consults; every reader defaults."""
    # Spelled with distinct lambda parameter names so this module's bag is not
    # a textual clone of another test's (each consumer owns its own defaults).
    return PipelineDependencies(
        get_content_cache=lambda: ContentCache(""),  # the ceiling never reads it
        compile_scope_regex_safe=lambda _compiled: None,
        fresh_seeds=lambda _seed, _cache, _policy: set(),
        fetch_and_decode=None,
        shared_http_cache=HttpCache(),
        resource_gate=ResourceGate(memory_ratio=lambda: 0.0, cpu_ratio=lambda: 0.0),  # idle by construction
    )


def _ceiling_probe_state() -> PipelineState:
    """The minimal state the ceiling call shape needs; its fields go unread."""
    # The dependencies block is spelled field-by-field (not a shared helper) so
    # this file stays the ceiling's only consumer of the never-read surface.
    return PipelineState(
        payload=CrawlRequest(url="http://probe.example/"),
        settings=CrawlerSettings.from_mapping(),
        url_pairs=[],
        policy=ProbeCrawlPolicy(),
        # The ceiling reads none of the fetch seams; their doubles are inert
        # and answer the same 200 twice so neither arm ever re-derives.
        fetcher=FetcherDouble(lambda *_: (200, b"", "text/html")),
        cffi=FetcherDouble(lambda *_: (200, b"", "text/html")),
        host_policy=UNRESTRICTED_HOST_POLICY,
        shutdown_event=asyncio.Event(),
        browser_worker=None,
        cache=None,
        content_cache=None,
        webhook_url=None,
        spill_store=_spill_store_never_used(),
        dependencies=_never_read_dependencies(),
    )
