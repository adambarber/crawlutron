"""Shared delta door-test harness: stores, baseline seeding, and the removed-verdict assert."""

from __future__ import annotations

import pathlib

from crawlutron.models.json_value import JsonValue
from crawlutron.storage.result_store import CrawlResultStore, CrawlSpillStore


def delta_stores(tmpdir: str) -> tuple[CrawlResultStore, CrawlSpillStore]:
    """Real result/spill stores over one throwaway directory."""
    root = pathlib.Path(tmpdir)
    return (
        CrawlResultStore(root / "results.db"),
        CrawlSpillStore(root / "spills.db"),
    )


def assert_removed_summary(summary: object, removed_url: str) -> dict[str, JsonValue]:
    """A delta summary reports exactly one removed URL under the skipped why."""
    assert isinstance(summary, dict)
    assert summary["removed"] == 1
    skipped = summary["skipped"]
    assert isinstance(skipped, list)
    assert {"url": removed_url, "why": "not_in_admitted_set"} in skipped
    return summary


def seed_baseline_row(
    result_store: CrawlResultStore,
    request_id: str,
    *,
    results: list[JsonValue],
    owner: str = "caller-a",
) -> None:
    """Store one baseline response payload the delta loader will read back."""
    result_store.put(
        request_id,
        {"request_id": request_id, "results": results},
        owner=owner,
    )


def baseline_row(
    *identities: tuple[str, str], markdown: str, etag: str | None = None
) -> dict[str, JsonValue]:
    """One baseline result carrying every identity the stored row keys under."""
    original, normalized = identities[0]
    return {
        "original_url": original,
        "normalized_url": normalized,
        "validators": {"etag": etag if etag is not None else '"v1"'},
        "markdown": markdown,
    }


__all__ = ["assert_removed_summary", "baseline_row", "delta_stores", "seed_baseline_row"]
