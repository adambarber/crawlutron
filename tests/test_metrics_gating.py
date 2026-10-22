"""#209: /metrics caller detail is a key-identity oracle.

Per-caller exposition is token-gated (CRAWLUTRON_KEY_ADMIN_TOKEN), labelled series
live in their own Prometheus families, and the caller dimension is overflow-capped.
"""

from __future__ import annotations

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.metrics_router import router as metrics_router
from app.runtime.metrics import MetricsStore, get_metrics
from app.storage.coordination_counters import CoordinationCounters


def _store(tmp_path) -> MetricsStore:
    return MetricsStore(counters=CoordinationCounters(tmp_path / "counters.db"))


def test_base_exposition_without_operator_token_omits_caller_detail(tmp_path):
    """The caller-labelled families are the oracle; a tokenless request is base only."""
    store = _store(tmp_path)
    store.observe_crawl(caller_id="key_a")
    store.observe_search(provider="tavily", caller_id="key_a")

    base = store.as_prometheus(caller_detail=False)
    assert "crawler_crawl_requests_total 1.0" in base
    assert "crawler_search_requests_total 1.0" in base
    assert "by_caller_total" not in base


def test_operator_token_path_exposes_caller_detail(tmp_path):
    store = _store(tmp_path)
    store.observe_crawl(caller_id="key_a", elapsed_ms=1.0)

    full = store.as_prometheus(caller_detail=True)
    assert 'crawler_crawl_requests_by_caller_total{caller="key_a"} 1.0' in full


def test_bare_and_caller_families_have_disjoint_names(tmp_path):
    """A9: one family must not carry two label sets -- bare and labelled differ by name."""
    store = _store(tmp_path)
    store.observe_crawl(caller_id="key_a")
    text = store.as_prometheus(caller_detail=True)
    # Bare and per-caller are different families, so no family mixes unlabelled + labelled.
    assert "crawler_crawl_requests_total " in text
    assert "crawler_crawl_requests_by_caller_total{" in text


def test_caller_label_dimension_is_capped_by_overflow_fold(tmp_path):
    """A8: past the cap, a new caller routes to one reserved series -- bare and sum
    stay exact; only attribution degrades."""
    store = _store(tmp_path)
    # 300 distinct callers, well beyond the configured cap; the label dimension
    # must stop growing (overflow fold) instead of minting a series per caller.
    for i in range(300):
        store.observe_crawl(caller_id=f"key_{i}")
    counts = store.snapshot().caller_counts
    # cap distinct series + one reserved overflow series -- bounded, not 300.
    assert len(counts) <= 257
    assert "__overflow__" in counts

    bare = store.snapshot().crawl_requests_total
    summed = sum(counts.values())
    assert bare == summed  # sum(labels) always equals bare


def test_search_and_crawl_caller_families_stay_distinct(tmp_path):
    """Item 3: crawl and search caller families must not bleed into each other."""
    store = _store(tmp_path)
    store.observe_crawl(caller_id="key_a")
    store.observe_search(provider="tavily", caller_id="key_a")

    # A search by key_a must not inflate its crawl family, nor vice versa.
    record = store.snapshot()
    assert record.caller_counts == {"key_a": 1.0}
    assert record.search_caller_counts == {"key_a": 1.0}
    text = store.as_prometheus(caller_detail=True)
    assert 'crawler_crawl_requests_by_caller_total{caller="key_a"} 1.0' in text
    assert 'crawler_search_requests_by_caller_total{caller="key_a"} 1.0' in text


def _bind_singleton(tmp_path) -> None:
    """Bind the metrics singleton to an isolated shared store via the public seam."""
    get_metrics().bind_counters(CoordinationCounters(tmp_path / "counters.db"))


async def _metrics_client(key_admin_token: str = "test-admin-token"):  # noqa: S107
    app = FastAPI()
    app.include_router(metrics_router)
    app.state.key_admin_token = key_admin_token
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def test_metrics_endpoint_gates_caller_detail_behind_operator_token(tmp_path):
    _bind_singleton(tmp_path)
    get_metrics().observe_crawl(caller_id="key_a")

    client = await _metrics_client()
    async with client:
        base = await client.get("/metrics")
        assert base.status_code == 200
        assert "crawler_crawl_requests_total 1.0" in base.text
        assert "by_caller_total" not in base.text

        gated = await client.get("/metrics", headers={"X-API-Key": "test-admin-token"})
        assert gated.status_code == 200
        assert 'crawler_crawl_requests_by_caller_total{caller="key_a"} 1.0' in gated.text

        wrong = await client.get("/metrics", headers={"X-API-Key": "wrong"})
        assert wrong.status_code == 200
        assert "by_caller_total" not in wrong.text
