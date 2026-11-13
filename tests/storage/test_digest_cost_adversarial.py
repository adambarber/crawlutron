"""Adversarial digest-cost pin for #764: per-save digest work is O(batch)."""

from __future__ import annotations

import pytest

from crawlutron.models.crawl_policy import CrawlPolicy
from crawlutron.models.json_value import JsonValue
from crawlutron.storage.job_store_accumulators import JobAccumulators
from tests._policy_factories import job_store as store_on


def _make_result(url: str) -> dict[str, JsonValue]:
    return {"url": url, "status": "success"}


@pytest.mark.parametrize("batch_size", [1, 4, 16])
def test_digest_work_is_proportional_to_batch_not_history(tmp_path, batch_size):
    """Digest work per save grows with batch size, not accumulated history."""
    store = store_on(tmp_path / "jobs.db")
    job = store.create("https://example.test/", CrawlPolicy())
    batches = 12

    # Pre-seed a large history so the O(history) defect would dominate.
    for i in range(500):
        job.results.append(_make_result(f"https://example.test/h{i}"))
    store.save(job)

    accumulator: JobAccumulators = store._accumulators
    per_save_bytes: list[int] = []
    for b in range(batches):
        before = accumulator.digest_work_bytes()
        for i in range(batch_size):
            job.results.append(_make_result(f"https://example.test/new{b * batch_size + i}"))
        store.save(job)
        per_save_bytes.append(accumulator.digest_work_bytes() - before)

    # Each save digests only the new tail: bytes per save bounded by C * batch_size.
    max_bytes = max(per_save_bytes)
    assert max_bytes <= 60 * batch_size + 500, (
        f"digest work per save is O(history), not O(batch): max {max_bytes} bytes for batch_size={batch_size}"
    )
