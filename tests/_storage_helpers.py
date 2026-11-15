"""Shared helpers for storage tests that assert against the table's truth."""

from __future__ import annotations

from crawlutron.models.crawl_policy import CrawlPolicy
from crawlutron.models.json_value import JsonValue


def _make_result(url: str) -> dict[str, JsonValue]:
    """One persisted page-history result."""
    return {"url": url, "status": "success"}


def _seed_history(store, count: int):
    """Create a job holding ``count`` results and persist it.

    Returns ``(job, expected)`` so callers assert against the same shape they
    seeded. Disjoint per test: each call builds its own store and job.
    """
    job = store.create("https://example.test/", CrawlPolicy())
    expected = [_make_result(f"https://example.test/h{i}") for i in range(count)]
    job.results.extend(expected)
    store.save(job)
    return job, expected
