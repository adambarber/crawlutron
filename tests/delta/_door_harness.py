"""Shared delta door-test harness: stores and the removed-verdict summary assert."""

from __future__ import annotations

import pathlib

from crawlutron.storage.result_store import CrawlResultStore, CrawlSpillStore


def delta_stores(tmpdir: str) -> tuple[CrawlResultStore, CrawlSpillStore]:
    """Real result/spill stores over one throwaway directory."""
    root = pathlib.Path(tmpdir)
    return (
        CrawlResultStore(root / "results.db"),
        CrawlSpillStore(root / "spills.db"),
    )


def assert_removed_summary(summary: object, removed_url: str) -> dict[str, object]:
    """A delta summary reports exactly one removed URL under the skipped why."""
    assert isinstance(summary, dict)
    assert summary["removed"] == 1
    skipped = summary["skipped"]
    assert isinstance(skipped, list)
    assert {"url": removed_url, "why": "not_in_admitted_set"} in skipped
    return summary


__all__ = ["assert_removed_summary", "delta_stores"]
