"""The whole-site batch loop must not re-decode the accumulators it owns.

#538: the loop's ``aload`` re-decoded the page history every batch, threw
it away, then re-attached its own; now it reloads state-only."""

import asyncio

import pytest

from app.api import job_runner
from app.config.settings import CrawlerSettings
from tests._api_harnesses import spy_on_history_reads
from tests._job_drivers import _seed_job
from tests._job_harnesses import ScriptedExecute
from tests._result_builders import _canned_result

_BATCH = 8


def _chain_execute(pages: int):
    """A scripted engine: page ``i`` discovers page ``i + 1``; forced multi-batch."""
    scripts = {}
    for i in range(pages):
        link = f"http://example.test/p{i + 1}" if i + 1 < pages else None
        scripts[f"http://example.test/p{i}"] = [
            _canned_result(f"http://example.test/p{i}", links=[link] if link else [])
        ]
    return ScriptedExecute(scripts=scripts)


@pytest.fixture(autouse=True)
def _fresh_runner():
    job_runner.reset_job_runner()
    yield
    job_runner.reset_job_runner()


async def test_batch_loop_never_reads_full_accumulators(job_store, monkeypatch):
    """A multi-batch job drives with exactly one full-accumulator read.

    The claim's full read stays; the loop reloads state-only."""
    pages = _BATCH * 4
    reached = spy_on_history_reads(monkeypatch)
    job_runner.install_execute_crawl(_chain_execute(pages))
    job = _seed_job(
        job_store,
        "http://example.test/p0",
        {"limits": {"max_depth": 0, "max_pages": 0, "respect_robots": False}},
        frontier=[f"http://example.test/p{i}" for i in range(pages)],
    )
    await job_runner.recover_orphaned_running_jobs(job_store)
    task = asyncio.ensure_future(
        job_runner.run_job(job.job_id, job_store, CrawlerSettings.from_mapping({}), asyncio.Event())
    )
    await asyncio.wait({task})

    finished = job_store.load(job.job_id)
    assert finished is not None and finished.status.value == "completed"
    assert finished.stats["total_crawled"] == pages, f"expected all {pages} pages crawled"
    assert len(finished.results) == pages, (
        f"expected all {pages} results published, got {len(finished.results)}"
    )
    # Exactly two full reads total: the runner's claim and this test's own row
    # read-back below already happened. Zero reads belong to the batch loop.
    assert reached == ["read_visited", "read_results"] * 2, (
        "the batch loop must not read full accumulators per batch: the only "
        f"full reads are the runner's claim and the test's own read-back, got {reached}"
    )
