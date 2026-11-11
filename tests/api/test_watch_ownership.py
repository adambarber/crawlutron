"""Owner-scoped watch registry: a watch is readable, listable, and deletable only
by the caller that registered it, and the anonymous bucket is bounded. Each test
drives the real router with an isolated key store.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from crawlutron.storage.caller_key_store import CallerKeyStore
from tests._app_harnesses import harness_app
from tests._watch_harnesses import (
    ASGITransport,
    AsyncClient,
    CrawlerSettings,
    get_settings,
    get_watch_store,
    watch_router,
)

TARGET = "http://example.com/feed.xml"
INTERVAL = 900


def _issue(keys: CallerKeyStore, label: str, *, max_pages: int = 0) -> str:
    """Mint a key directly into ``keys`` and return its secret."""
    return keys.issue_key(label, max_pages=max_pages).secret


@asynccontextmanager
async def _keyed_watch_client(
    watch_store, key_store: CallerKeyStore, *, max_watches: int = 100
) -> AsyncGenerator[AsyncClient, None]:
    """A client over ``/v1/watches`` whose app holds ``key_store`` for identity."""
    app = harness_app(watch_router)
    app.state.caller_key_store = key_store
    app.dependency_overrides[get_watch_store] = lambda: watch_store
    app.dependency_overrides[get_settings] = lambda: CrawlerSettings.from_mapping(
        {"CRAWLUTRON_MAX_WATCHES_PER_CALLER": str(max_watches)}
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def _register(client, headers: dict[str, str], url: str) -> dict[str, object]:
    """Create one watch for a caller and return its parsed body."""
    resp = await client.post("/v1/watches", headers=headers, json={"url": url, "interval_seconds": INTERVAL})
    assert resp.status_code == 201, resp.text
    return resp.json()


@asynccontextmanager
async def _two_tenant_client(watch_store, tmp_path):
    """Open the keyed client with two distinct issuers; yield us and their keys."""
    keys = CallerKeyStore(tmp_path / "keys.db")
    alice = _issue(keys, "alice")
    bob = _issue(keys, "bob")
    async with _keyed_watch_client(watch_store, keys) as client:
        yield client, alice, bob


async def test_a_caller_cannot_read_or_delete_another_callers_watch(watch_store, tmp_path):
    async with _two_tenant_client(watch_store, tmp_path) as (client, alice, bob):
        created = await _register(client, {"X-API-Key": alice}, TARGET)
        watch_id = created["watch_id"]

        read = await client.get(f"/v1/watches/{watch_id}", headers={"X-API-Key": bob})
        removed = await client.delete(f"/v1/watches/{watch_id}", headers={"X-API-Key": bob})

    assert read.status_code == 404, read.text
    assert removed.status_code == 404, removed.text
    # Alice's row survives Bob's deletion attempt.
    assert watch_store.get(watch_id) is not None


async def test_listing_is_owner_scoped(watch_store, tmp_path):
    async with _two_tenant_client(watch_store, tmp_path) as (client, alice, bob):
        for url in (TARGET, "http://example.com/other"):
            await _register(client, {"X-API-Key": alice}, url)
        await _register(client, {"X-API-Key": bob}, "http://example.net/bob")

        alice_listing = await client.get("/v1/watches", headers={"X-API-Key": alice})
        bob_listing = await client.get("/v1/watches", headers={"X-API-Key": bob})

    assert {row["url"] for row in alice_listing.json()["watches"]} == {TARGET, "http://example.com/other"}
    assert {row["url"] for row in bob_listing.json()["watches"]} == {"http://example.net/bob"}


async def test_a_keyed_watch_is_stamped_with_its_owner(watch_store, tmp_path):
    keys = CallerKeyStore(tmp_path / "keys.db")
    alice = _issue(keys, "alice")
    async with _keyed_watch_client(watch_store, keys) as client:
        created = await _register(client, {"X-API-Key": alice}, TARGET)

    assert created["owner"]  # non-empty owner for a keyed caller
    assert watch_store.list_for_caller(created["owner"])


async def test_the_anonymous_bucket_is_bounded_by_a_per_caller_cap(watch_store, tmp_path):
    keys = CallerKeyStore(tmp_path / "keys.db")
    async with _keyed_watch_client(watch_store, keys, max_watches=1) as client:
        # One live watch then the next registration hits the operator cap.
        await client.post("/v1/watches", json={"url": TARGET, "interval_seconds": INTERVAL})
        over = await client.post(
            "/v1/watches",
            json={"url": "http://example.com/over", "interval_seconds": INTERVAL},
        )

    assert over.status_code == 429, over.text
    assert over.json()["code"] == "watch_limit_exceeded"


async def test_an_interval_below_the_politeness_floor_is_refused(watch_store, tmp_path):
    keys = CallerKeyStore(tmp_path / "keys.db")
    async with _keyed_watch_client(watch_store, keys) as client:
        response = await client.post("/v1/watches", json={"url": TARGET, "interval_seconds": 1})

    assert response.status_code == 422, response.text
    assert watch_store.list_watches() == []
