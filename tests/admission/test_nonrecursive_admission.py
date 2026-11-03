"""The sync (non-recursive) batch path must pace itself through the same
RAM-weighted admission gate the recursive path uses -- not run RAM-blind
against a 256-connection hardcode (#350)."""

import pytest

from crawlutron.orchestration import request_pipeline
from crawlutron.orchestration.engine import crawl_request
from crawlutron.runtime.pressure_memory import MemoryStats
from crawlutron.runtime.pressure_sizing import WeightedAdmission
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


async def _admission_spy(monkeypatch):
    """Install a spy on ``WeightedAdmission.admit`` and return the captures list
    of ``(want_http, want_browser)`` tuples the dispatch path asks for."""
    admits: list[tuple[int, int]] = []
    original_admit = WeightedAdmission.admit

    async def spy_admit(self, want_http: int, want_browser: int) -> tuple[int, int]:
        admits.append((want_http, want_browser))
        return await original_admit(self, want_http, want_browser)

    monkeypatch.setattr(WeightedAdmission, "admit", spy_admit)
    return admits


@pytest.mark.parametrize(
    ("policy", "want_http_must_be_zero"),
    [
        # Default chain contains a browser strategy -> unpinned jobs are
        # admitted against browser capacity ((0, N)), never HTTP ((N, 0)).
        ({"limits": {"max_depth": 0}}, True),
        # Chain with no browser link -> admitted as HTTP ((N, 0)).
        ({"limits": {"max_depth": 0}, "strategies": ["HTTP_DECLARED_CRAWLER"]}, False),
    ],
)
async def test_nonrecursive_admission_charge_matches_policy_chain(
    monkeypatch, policy, want_http_must_be_zero
):
    """#595: the non-recursive batch is admitted per tier per the policy chain,
    so browser-capable jobs are not silently lumped into HTTP admission."""
    admits = await _admission_spy(monkeypatch)
    _pin_small_box(monkeypatch)

    fetcher = FetcherDouble(_page_route)
    urls = [f"http://site{i}.example/page" for i in range(3)]
    out = await crawl_request(
        {"urls": urls, "policy": policy},
        http_fetcher=fetcher,
        cffi_fetcher=fetcher,
        browser_worker=None,
    )

    assert len(out["results"]) == len(urls), "every submitted page must return a result"
    assert admits, "the sync batch must consult the WeightedAdmission gate"
    if want_http_must_be_zero:
        assert all(want_http == 0 for want_http, _ in admits), (
            f"browser-capable jobs were lumped into HTTP admission: {admits}"
        )
    else:
        assert all(want_browser == 0 for _, want_browser in admits), (
            f"http-only jobs were charged to browser admission: {admits}"
        )
