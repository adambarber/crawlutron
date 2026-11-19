"""Page-first job listing: LIMIT binds at the crawl_jobs scan, counts never run."""

from __future__ import annotations

from crawlutron.models.crawl_policy import CrawlPolicy
from crawlutron.storage.job_store import JobStore
from crawlutron.storage.job_store_accumulators import JobAccumulators
from tests._policy_factories import UNRESTRICTED_HOST_POLICY, deployment_tier


def test_counts_by_job_is_deleted():
    """``counts_by_job()`` and its whole-table aggregate are removed (#1131)."""
    assert not hasattr(JobAccumulators, "counts_by_job")


def test_list_jobs_applies_limit_at_sql_level():
    """A limited listing returns at most ``limit`` rows, newest first, without
    running the whole-table counts aggregate (it no longer exists)."""
    store: JobStore = JobStore(":memory:", policy=UNRESTRICTED_HOST_POLICY, tier=deployment_tier())
    try:
        for _ in range(20):
            store.create("http://example.com/", CrawlPolicy())

        page = store.list_jobs(limit=5)
        assert len(page) == 5
        # Newest first: created_at DESC, job_id DESC breaks ties.
        assert all(page[i]["created_at"] >= page[i + 1]["created_at"] for i in range(len(page) - 1))
        # Per-job counts read as zero on listing (no per-job count query).
        assert all(row["visited_size"] == 0 and row["results_count"] == 0 for row in page)
    finally:
        store.close()


def test_list_jobs_honors_offset():
    """Offset skips rows at the SQL level, page-first."""
    store: JobStore = JobStore(":memory:", policy=UNRESTRICTED_HOST_POLICY, tier=deployment_tier())
    try:
        ids = [store.create("http://example.com/", CrawlPolicy()).job_id for _ in range(6)]
        first = store.list_jobs(limit=2, offset=0)
        second = store.list_jobs(limit=2, offset=2)
        assert [row["job_id"] for row in first] != [row["job_id"] for row in second]
        assert {row["job_id"] for row in first} | {row["job_id"] for row in second} <= set(ids)
    finally:
        store.close()
