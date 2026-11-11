"""Result retrieval by request_id (fixes dead status_url + lost sync results)."""

import pytest

from crawlutron.models.json_value import JsonValue
from crawlutron.storage.result_store import CrawlResultStore
from tests._clocks import advance_clock, arrange_clock


def _payload(tag="x"):
    return {"request_id": f"crawl_{tag}", "results": [{"status": "success"}]}


def test_roundtrip():
    state, clock = arrange_clock()
    store = CrawlResultStore(":memory:", ttl_seconds=3600, max_entries=10, clock=clock)
    store.put("crawl_a", _payload("a"))
    assert store.get("crawl_a") == _payload("a")


def test_expiry_returns_none():
    state, clock = arrange_clock()
    store = CrawlResultStore(":memory:", ttl_seconds=600, max_entries=10, clock=clock)
    store.put("crawl_a", _payload("a"))
    advance_clock(state, 601)
    assert store.get("crawl_a") is None


def test_max_entries_prunes_oldest():
    _state, clock = arrange_clock()
    store = CrawlResultStore(":memory:", ttl_seconds=3600, max_entries=3, clock=clock)
    for i in range(5):
        store.put(f"crawl_{i}", _payload(str(i)))
    kept = [store.get(f"crawl_{i}") is not None for i in range(5)]
    assert sum(kept) == 3, kept


def test_missing_id_returns_none():
    _state, clock = arrange_clock()
    store = CrawlResultStore(":memory:", ttl_seconds=3600, max_entries=10, clock=clock)
    assert store.get("crawl_nope") is None


async def test_status_url_roundtrip_via_api(async_client):
    """The 202-advertised status_url now actually works end to end."""
    crawl_resp = await async_client.post(
        "/v1/crawl",
        json={"url": "http://127.0.0.1:1/x", "policy": {"strategies": ["HTTP_DECLARED_CRAWLER"]}},
    )
    assert crawl_resp.status_code == 200, crawl_resp.text
    request_id = crawl_resp.json()["request_id"]
    status = await async_client.get(f"/v1/crawl/{request_id}")
    assert status.status_code == 200, status.text
    body = status.json()
    assert body["request_id"].startswith("crawl_")
    assert "results" in body

    # Unknown id -> 404
    missing = await async_client.get("/v1/crawl/crawl_doesnotexist")
    assert missing.status_code == 404


def test_a_payload_past_the_byte_cap_is_refused_not_retained(tmp_path):
    """#443: the write edge refuses an oversized payload before persistence."""
    from crawlutron.storage.result_store import PayloadTooLargeError

    store = CrawlResultStore(tmp_path / "cap.db", max_payload_bytes=128)
    oversized: dict[str, JsonValue] = {"request_id": "crawl_huge", "results": [], "pad": "x" * 512}

    with pytest.raises(PayloadTooLargeError):
        store.put("crawl_huge", oversized)

    assert store.get("crawl_huge") is None, "a refused payload was retained anyway"


def test_a_payload_within_the_byte_cap_is_retained(tmp_path):
    """The cap refuses the oversized, not the ordinary: a crawl response that
    fits must still round-trip after the write edge learns to measure."""
    store = CrawlResultStore(tmp_path / "cap.db", max_payload_bytes=1024)
    payload: dict[str, JsonValue] = {
        "request_id": "crawl_ok",
        "results": [{"status": "success"}],
        "pad": "y" * 128,
    }

    store.put("crawl_ok", payload)

    assert store.get("crawl_ok") == payload


def test_page_reads_are_bounded_and_continue_by_cursor():
    """#443: a page read returns at most ``limit`` rows and a cursor that
    continues the scan; the whole table is never materialized at once."""
    _state, clock = arrange_clock()
    store = CrawlResultStore(":memory:", ttl_seconds=3600, max_entries=10, clock=clock)
    for i in range(5):
        store.put(f"crawl_{i}", _payload(str(i)))

    # One stored_at for every row, so the scan is discriminated by request_id:
    # the cursor must carry both or the ties swallow rows.
    first = store.payloads_page(limit=2)
    second = store.payloads_page(limit=2, cursor=first[-1][0])
    third = store.payloads_page(limit=2, cursor=second[-1][0])

    ids = []
    for _, page_payload in first + second + third:
        assert isinstance(page_payload, dict)
        request_id = page_payload["request_id"]
        assert isinstance(request_id, str)
        ids.append(request_id)
    assert ids == ["crawl_4", "crawl_3", "crawl_2", "crawl_1", "crawl_0"], ids
    assert store.payloads_page(limit=2, cursor=third[-1][0]) == [], "the scan did not end"


def test_retained_count_is_the_ttl_window_size():
    """The honest denominator: how many rows the TTL window still holds."""
    state, clock = arrange_clock()
    store = CrawlResultStore(":memory:", ttl_seconds=300, max_entries=10, clock=clock)
    store.put("crawl_kept", _payload("kept"))
    store.put("crawl_gone", _payload("gone"))
    advance_clock(state, 301)
    store.put("crawl_late", _payload("late"))

    assert store.retained_count() == 1
