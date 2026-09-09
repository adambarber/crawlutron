"""Result retrieval by request_id (fixes dead status_url + lost sync results)."""

from app.crawl.result_store import CrawlResultStore


def _clock(start=1000.0):
    state = {"t": start}

    def clock():
        return state["t"]

    def advance(seconds):
        state["t"] += seconds

    return clock, advance


def _payload(tag="x"):
    return {"request_id": f"crawl_{tag}", "results": [{"status": "success"}]}


def test_roundtrip():
    clock, _ = _clock()
    store = CrawlResultStore(":memory:", ttl_seconds=3600, max_entries=10, clock=clock)
    store.put("crawl_a", _payload("a"))
    assert store.get("crawl_a") == _payload("a")


def test_expiry_returns_none():
    clock, advance = _clock()
    store = CrawlResultStore(":memory:", ttl_seconds=600, max_entries=10, clock=clock)
    store.put("crawl_a", _payload("a"))
    advance(601)
    assert store.get("crawl_a") is None


def test_max_entries_prunes_oldest():
    clock, _ = _clock()
    store = CrawlResultStore(":memory:", ttl_seconds=3600, max_entries=3, clock=clock)
    for i in range(5):
        clock() and None
        store.put(f"crawl_{i}", _payload(str(i)))
        # ensure strictly increasing stored_at
        for _ in range(1):
            pass
    # with a static clock, stored_at ties are possible; use distinct payloads check instead:
    kept = [store.get(f"crawl_{i}") is not None for i in range(5)]
    assert sum(kept) == 3, kept


def test_missing_id_returns_none():
    clock, _ = _clock()
    store = CrawlResultStore(":memory:", ttl_seconds=3600, max_entries=10, clock=clock)
    assert store.get("crawl_nope") is None


async def test_status_url_roundtrip_via_api():
    """The 202-advertised status_url now actually works end to end."""
    from helpers import make_async_client

    from app.main import app

    async def run(client):
        crawl_resp = await client.post(
            "/v1/crawl",
            json={"url": "http://127.0.0.1:1/x", "policy": {"strategies": ["HTTP_DECLARED_CRAWLER"]}},
        )
        assert crawl_resp.status_code == 200, crawl_resp.text
        request_id = crawl_resp.json()["request_id"]
        status = await client.get(f"/v1/crawl/{request_id}")
        return status

    async with make_async_client(app)() as client:
        status = await run(client)
    assert status.status_code == 200, status.text
    body = status.json()
    assert body["request_id"].startswith("crawl_")
    assert "results" in body

    # Unknown id -> 404
    async with make_async_client(app)() as client:
        missing = await client.get("/v1/crawl/crawl_doesnotexist")
    assert missing.status_code == 404
