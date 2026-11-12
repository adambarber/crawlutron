"""Hermetic benchmark of per-request in-memory spill setup: the construct+migrate+release+close lifecycle and
end-to-end crawl overhead, private vs shared pre-migrated store. Local fetch doubles, zero-yield sleeps --
the reported setup share is a conservative upper bound on materiality. Prints markdown and JSON.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path
from unittest.mock import patch

import crawlutron.runtime.process_state as process_state
from crawlutron.config.settings import CrawlerSettings
from crawlutron.orchestration.engine import crawl_request
from crawlutron.storage.result_store import CrawlSpillStore
from crawlutron.transport.cache import HttpCache
from tests._fetch_doubles import FetcherDouble, make_fetch

_PAGES = (1, 10, 100)
_REPEATS = 60
_STORE_ITERS = 500


def _answer(url, headers, timeout_seconds, max_content_length=0):
    """Hermetic fetch answer: every page returns a small, identical HTML body."""
    return make_fetch(status=200, body=b"<html><body>hello</body></html>")


def _settings() -> CrawlerSettings:
    """Settings permissive enough for a 100-URL batch and fast local fetches."""
    return CrawlerSettings.from_mapping({"CRAWLUTRON_MAX_BATCH_SIZE": "200"})


def _pin_sleeps() -> None:
    """Zero-yield sleeps: removes scheduler politeness delay so crawl-body time
    reflects fetch work only (which is instant here), maximising spill share."""
    _real_asyncio_sleep = asyncio.sleep

    async def _fast_asyncio_sleep(delay, *args, **kwargs):
        del delay, args, kwargs
        await _real_asyncio_sleep(0)

    patch("asyncio.sleep", _fast_asyncio_sleep).start()
    patch("time.sleep", lambda *a, **k: None).start()


async def _run_crawl(
    pages: int,
    spill_store: CrawlSpillStore | None,
    settings: CrawlerSettings,
) -> None:
    """One sync crawl of ``pages`` local pages through ``crawl_request``."""
    urls = [f"http://example.com/page/{i}" for i in range(pages)]
    fetcher = FetcherDouble(_answer)
    await crawl_request(
        {"urls": urls, "policy": {"limits": {"max_pages": pages}}},
        settings=settings,
        http_fetcher=fetcher,
        cffi_fetcher=fetcher,
        cache=HttpCache(),
        spill_store=spill_store,
    )


async def _bench_crawl_async(
    pages: int,
    make_store,
    settings: CrawlerSettings,
    repeats: int,
) -> list[float]:
    """Run ``repeats`` crawls of ``pages`` pages in one event loop, timed each; ``make_store`` returns the
    crawl's spill store, built outside the timed region. ``None`` is the private path -- the store is
    built inside the timed region.
    """
    times: list[float] = []
    for _ in range(repeats):
        store = make_store()
        start = time.perf_counter()
        await _run_crawl(pages, store, settings)
        times.append(time.perf_counter() - start)
        if store is not None:
            store.close()
        process_state.reset_all_process_state()
    return times


def _bench_crawl(pages: int, make_store, settings: CrawlerSettings, repeats: int) -> list[float]:
    return asyncio.run(_bench_crawl_async(pages, make_store, settings, repeats))


def _bench_spill_lifecycle(iterations: int) -> list[float]:
    """Time the direct per-request spill lifecycle: construct + migrate, fence
    release, close. This is the exact per-crawl spill tax, independent of crawl
    body or global-state accumulation."""
    times: list[float] = []
    for _ in range(iterations):
        start = time.perf_counter()
        store = CrawlSpillStore(":memory:")
        store.release_spill("bench-request")
        store.close()
        times.append(time.perf_counter() - start)
    return times


def _stats(times: list[float]) -> dict[str, float]:
    return {
        "min_ms": min(times) * 1000.0,
        "mean_ms": statistics.mean(times) * 1000.0,
        "median_ms": statistics.median(times) * 1000.0,
        "max_ms": max(times) * 1000.0,
        "stdev_ms": statistics.pstdev(times) * 1000.0 if len(times) > 1 else 0.0,
    }


def _bench_crawl_matrix(settings: CrawlerSettings, repeats: int) -> list[dict[str, object]]:
    """End-to-end crawl overhead (b): private per-crawl store vs shared store,
    one row per page count. A fresh shared store per repeat keeps the shared
    body from being inflated by accumulated rows."""
    rows: list[dict[str, object]] = []
    for pages in _PAGES:
        private_times = _bench_crawl(pages, lambda: None, settings, repeats)
        shared_times = _bench_crawl(pages, lambda: CrawlSpillStore(":memory:"), settings, repeats)
        private = _stats(private_times)
        shared_stats = _stats(shared_times)
        tax_ms = private["mean_ms"] - shared_stats["mean_ms"]
        tax_pct = (tax_ms / private["mean_ms"] * 100.0) if private["mean_ms"] > 0 else 0.0
        rows.append(
            {
                "pages": pages,
                "private_mean_ms": private["mean_ms"],
                "shared_mean_ms": shared_stats["mean_ms"],
                "spill_tax_ms": tax_ms,
                "spill_tax_pct_of_crawl": tax_pct,
                "private": private,
                "shared": shared_stats,
            }
        )
    return rows


def _render_report(
    args: argparse.Namespace, lifecycle_stats: dict[str, float], rows: list[dict[str, object]]
) -> None:
    print("# Spill setup benchmark (hermetic, no real network)", flush=True)
    print(flush=True)
    print("## (a) Direct per-request spill lifecycle: construct + migrate + release + close", flush=True)
    print(f"repeats: {args.store_iters}", flush=True)
    print(
        f"min={lifecycle_stats['min_ms']:.3f} ms  mean={lifecycle_stats['mean_ms']:.3f} ms  "
        f"median={lifecycle_stats['median_ms']:.3f} ms  max={lifecycle_stats['max_ms']:.3f} ms  "
        f"pstdev={lifecycle_stats['stdev_ms']:.3f} ms",
        flush=True,
    )
    print(flush=True)
    print("## (b) End-to-end sync crawl: private per-crawl store vs shared store", flush=True)
    print("sleeps pinned to zero-yield; crawl body is fetch-work only", flush=True)
    print(flush=True)
    print(
        "| pages | private ms | shared ms | tax ms/crawl | tax % of crawl |",
        flush=True,
    )
    print(
        "|-------|--------------|-----------|--------------|----------------|",
        flush=True,
    )
    for row in rows:
        print(
            f"| {row['pages']} | {row['private_mean_ms']:.3f} | {row['shared_mean_ms']:.3f} | "
            f"{row['spill_tax_ms']:.3f} | {row['spill_tax_pct_of_crawl']:.2f}% |",
            flush=True,
        )
    print(flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark per-request spill store setup cost")
    parser.add_argument("--repeats", type=int, default=_REPEATS, help="repetitions per crawl config")
    parser.add_argument("--store-iters", type=int, default=_STORE_ITERS, help="spill-lifecycle repetitions")
    parser.add_argument("--json", type=Path, default=None, help="write results as JSON to this path")
    args = parser.parse_args()

    _pin_sleeps()
    settings = _settings()

    # (a) Direct per-request spill lifecycle: construct + migrate + release + close.
    lifecycle_stats = _stats(_bench_spill_lifecycle(args.store_iters))

    # (b) End-to-end crawl overhead: private per-crawl store vs shared store.
    rows = _bench_crawl_matrix(settings, args.repeats)

    report: dict[str, object] = {
        "spill_lifecycle_ms": lifecycle_stats,
        "spill_lifecycle_repeats": args.store_iters,
        "crawl_repeats": args.repeats,
        "pages": list(_PAGES),
        "rows": rows,
    }

    _render_report(args, lifecycle_stats, rows)

    if args.json is not None:
        args.json.write_text(json.dumps(report, indent=2) + "\n")
        print(f"wrote {args.json}", flush=True)


if __name__ == "__main__":
    main()
