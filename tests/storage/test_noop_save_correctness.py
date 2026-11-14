"""Correctness parity: shrunken history resyncs correctly.

#980: Verify that shrunken history is handled correctly after the no-op optimization.
"""

from __future__ import annotations

import sqlite3

from crawlutron.models.crawl_policy import CrawlPolicy
from tests._policy_factories import canonical
from tests._policy_factories import job_store as store_on


def test_shrunken_history_resyncs_correctly(tmp_path):
    """A job whose history shrank below the persisted mark must resync.

    The resync path clears the table and re-inserts from mark 0.
    """
    db = tmp_path / "jobs.db"
    store = store_on(db)
    job = store.create("https://example.test/", CrawlPolicy())

    for i in range(20):
        url = f"https://example.test/h{i}"
        job.results.append({"url": url, "status": "success"})
        job.visited.add(canonical(url))
    store.save(job)

    job.results = job.results[:5]
    store.save(job)

    conn = sqlite3.connect(db)
    count = conn.execute("SELECT count(*) FROM crawl_job_results WHERE job_id = ?", (job.job_id,)).fetchone()[
        0
    ]
    conn.close()

    assert count == 5, f"expected 5 result rows after shrink, got {count}"
    store._conn.close()
