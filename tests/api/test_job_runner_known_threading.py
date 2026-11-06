"""Red-first probes for #548: the occupied set is threaded across the job loop.

Main built the whole occupied set per batch twice -- once unconditionally in
``_pop_frontier`` (``set(job.visited) | {in_flight}`` before the guard that is
its only consumer) and once in ``_absorb_batch`` (a fresh
``admitted_page_identities`` rebuild). Both are killed by seeding ``known`` once
in ``run_job`` and threading it through the loop; these tests pin that.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest

from crawlutron.api import job_runner
from crawlutron.models.crawl_policy import CrawlPolicy
from crawlutron.models.crawl_result import EngineCrawlResult
from crawlutron.models.normalized_url import NormalizedUrl
from crawlutron.models.result_record import ProducerEnvelope
from crawlutron.storage.job_store import CrawlJobState, FrontierEntry, JobStatus, JobStore
from crawlutron.storage.job_store_collections import JobKind, OrderedUrlSet
from tests._policy_factories import canonical


def _make_state(frontier_pages: int = 10) -> CrawlJobState:
    """Build a live unlimited job with a populated frontier."""
    state = CrawlJobState(
        job_id="test-job",
        kind=JobKind.WHOLE_SITE,
        root_url="https://example.com/",
        policy=CrawlPolicy(),
        status=JobStatus.RUNNING,
    )
    state.host_policy = MagicMock()
    state.tier = MagicMock()
    state.policy = MagicMock()
    state.policy.limits = MagicMock(max_pages=0, max_depth=1)  # unlimited
    state.root_url = "https://example.com/"
    state.frontier = [
        FrontierEntry(url=canonical(f"https://example.com/p{i}"), depth=1) for i in range(frontier_pages)
    ]
    state.visited = OrderedUrlSet()
    state.visited_mark = 0
    state.in_flight = []
    state.transient_retries = {}
    state.results = []
    state.stats = {}
    return state


def _make_engine_result(url: str) -> EngineCrawlResult:
    """Build a minimal EngineCrawlResult for a successful crawl."""
    return EngineCrawlResult(
        original_url=url,
        normalized_url=canonical(url),
        final_url=canonical(url),
        status="success",
        classification="rendered",
        selected_strategy="HTTP_DECLARED_CRAWLER",
        attempted_strategies=(),
        http_status=200,
        headers={},
        content_type="text/html",
        html="<html>test</html>",
        text="test",
        extraction_method="trafilatura",
        quality_score=1.0,
        discovered_links=(),
        error=None,
    )


def _make_resolved_batch(batch: list[FrontierEntry]) -> dict[NormalizedUrl, EngineCrawlResult]:
    """Build a resolved dict with successful results for each entry."""
    return {entry.url: _make_engine_result(entry.url.raw) for entry in batch}


class _SpyExecuteCrawl:
    """Seam replacement that tracks calls and returns canned results."""

    def __init__(self, *batches: list[EngineCrawlResult]):
        self.batches = list(batches)
        self.call_count = 0

    async def __call__(self, payload_dict, *, settings, shutdown_event) -> ProducerEnvelope:
        self.call_count += 1
        idx = min(self.call_count - 1, len(self.batches) - 1)
        results = self.batches[idx] if self.batches else []
        return {
            "request_id": "spy-job",
            "submitted_count": len(results),
            "policy_schema_version": "1",
            "results": results,
        }


def _loop_store(state: CrawlJobState) -> MagicMock:
    """A store mock that serves ``state`` for every reload (no persistence)."""

    async def areload_state(job_id: str):
        return state

    store = MagicMock(spec=JobStore)
    store.aload = AsyncMock(return_value=state)
    store.areload_state = areload_state
    store.asave = AsyncMock()
    store.aset_status = AsyncMock(return_value=True)
    return store


@pytest.mark.asyncio
async def test_known_is_threaded_not_rebuilt_per_batch(tmp_path, monkeypatch):
    """#548: admitted_page_identities is called ONCE (seeded before the loop)
    and the loop threads its own known set. Main rebuilt it per batch (RED).
    """
    frontier_pages = 24
    state = _make_state(frontier_pages=frontier_pages)

    batch_results = [
        list(_make_resolved_batch(state.frontier[i : i + job_runner.BATCH_SIZE]).values())
        for i in range(0, frontier_pages, job_runner.BATCH_SIZE)
    ]

    store = _loop_store(state)
    executor = _SpyExecuteCrawl(*batch_results)
    job_runner.install_execute_crawl(executor)

    admitted_calls: list[int] = []
    original_admitted = job_runner.admitted_page_identities

    def counting_admitted(job, *, host_policy):
        admitted_calls.append(1)
        return original_admitted(job, host_policy=host_policy)

    monkeypatch.setattr(job_runner, "admitted_page_identities", counting_admitted)

    try:
        await job_runner.run_job(
            job_id="test-job",
            job_store=store,
            settings=MagicMock(),
            shutdown_event=asyncio.Event(),
        )
    finally:
        job_runner.reset_execute_crawl()

    # The loop must actually have run to completion: three batches crawled.
    assert executor.call_count == 3, f"loop ran {executor.call_count} batches, expected 3"
    assert len(state.visited) == frontier_pages
    print(f"admitted_page_identities calls: {len(admitted_calls)}")
    assert len(admitted_calls) <= 1, (
        f"admitted_page_identities called {len(admitted_calls)} times "
        f"(expected 1 seeded before the loop; main rebuilds it per batch)"
    )


@pytest.mark.asyncio
async def test_pop_frontier_no_set_build_on_unlimited_job(tmp_path, monkeypatch):
    """#548: _pop_frontier does NOT build set(visited) when max_pages=0 -- the
    set was built before the ``max_pages > 0`` guard that is its only consumer.
    Main builds it unconditionally whenever in_flight is non-empty (RED).
    """
    state = _make_state(frontier_pages=10)
    state.in_flight = [
        FrontierEntry(url=canonical(f"https://example.com/flight{i}"), depth=1) for i in range(3)
    ]

    original_set = set
    set_builds = []

    def traced_set(iterable=(), /):
        if iterable is state.visited:
            set_builds.append("visited")
        return original_set(iterable)

    monkeypatch.setattr("builtins.set", traced_set)

    for _ in range(3):
        job_runner.pop_frontier(state)

    print(f"set() builds on visited: {len(set_builds)}")
    assert len(set_builds) == 0, (
        f"_pop_frontier built set(visited) {len(set_builds)} times on an "
        f"unlimited job (max_pages=0); the build is dead work before the "
        f"max_pages guard. tests/api/test_job_runner_known_threading.py::"
        f"test_pop_frontier_no_set_build_on_unlimited_job"
    )


@pytest.mark.asyncio
async def test_transient_requeue_survives_known_threading(tmp_path):
    """#548: a transient entry must still be requeued under the threaded known
    set. The caller discards the identity from ``known`` before requeueing so
    the retry can re-admit; the frontier must end with exactly one copy (#558:
    production pops the batch out of the frontier first, so the seeded copy is
    removed here to mirror that)."""
    state = _make_state(frontier_pages=1)
    entry = state.frontier[0]
    state.frontier.clear()  # _pop_frontier removed the batch; absorb requeues it
    resolved = {entry.url: _make_engine_result(entry.url.raw)}
    resolved[entry.url] = replace(
        _make_engine_result(entry.url.raw),
        classification="connection_error",  # transient
        status="failed",
        error="connection_error",
    )
    state.in_flight = [entry]
    known = {entry.url, canonical("https://example.com/other")}

    failed = await job_runner.absorb_batch(state, [entry], resolved, MagicMock(), known=known)

    assert failed == 0
    assert [e.url for e in state.frontier] == [entry.url], (
        "a transient entry was dropped from the frontier under the threaded "
        "known set; it must be requeued for retry "
        "(tests/api/test_job_runner_known_threading.py::"
        "test_transient_requeue_survives_known_threading)"
    )
    assert state.transient_retries.get(entry.url) == 1
    # The caller discarded it so the requeue could re-admit; the requeue
    # re-occupies the set (the retry is queued, so it counts against the cap).
    assert entry.url in known


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
