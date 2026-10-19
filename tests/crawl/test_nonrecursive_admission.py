"""The sync (non-recursive) batch path must pace itself through the same
RAM-weighted admission gate the recursive path uses -- not run RAM-blind
against a 256-connection hardcode (#350)."""

from app.orchestration import request_pipeline
from app.orchestration.engine import crawl_request
from app.runtime.memory_pressure import MemoryStats
from app.runtime.resource_pressure import WeightedAdmission
from tests._fetch_doubles import FetcherDouble


def _page_route(url, sent_headers, timeout, max_content_length):
    return {
        "status": 200,
        "body": b"<html><body>hello world, enough text to render</body></html>",
        "ctype": "text/html; charset=utf-8",
    }


def _pin_small_box(monkeypatch) -> None:
    """Derive a tiny RAM ceiling so the admission gate clearly binds the batch."""
    monkeypatch.setattr(
        request_pipeline,
        "read_memory_stats",
        lambda: MemoryStats(total_bytes=1 << 30, used_bytes=0),
    )
    monkeypatch.setattr(request_pipeline, "derive_browser_pool_size", lambda: 2)


async def test_nonrecursive_batch_consults_weighted_admission(monkeypatch):
    """The sync batch must reach the same WeightedAdmission gate the recursive
    path uses. Probe the gate directly: before the fix the non-recursive path
    hands the whole batch to the scheduler and never builds the admission."""
    admits: list[tuple[int, int]] = []
    original_admit = WeightedAdmission.admit

    async def spy_admit(self, want_http: int, want_browser: int) -> tuple[int, int]:
        admits.append((want_http, want_browser))
        return await original_admit(self, want_http, want_browser)

    monkeypatch.setattr(WeightedAdmission, "admit", spy_admit)
    _pin_small_box(monkeypatch)

    fetcher = FetcherDouble(_page_route)
    urls = [f"http://site{i}.example/page" for i in range(3)]
    out = await crawl_request(
        {"urls": urls, "policy": {"limits": {"max_depth": 0}}},
        http_fetcher=fetcher,
        cffi_fetcher=fetcher,
        browser_worker=None,
    )

    assert len(out["results"]) == len(urls)
    assert admits, (
        "the sync batch dispatched without consulting the WeightedAdmission gate the recursive path uses"
    )
