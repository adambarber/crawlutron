"""``/v1/watches``: registration, status, listing and deletion.

Registration is the only edge here that decides anything -- it admits the URL
through the deployment's policy and persists canonical identity. Everything
the status endpoints report was written by the sweep, so these tests write
through the store and read back through the API.
"""

import pytest
from httpx import Response

from app.models.content_validators import ContentValidators
from app.models.watch_outcome import WatchOutcome
from tests._assertions import block_private_targets
from tests._policy_factories import canonical
from tests._watch_harnesses import watch_client

TARGET = "http://example.com/feed.xml"
INTERVAL = 900
WEBHOOK = "http://127.0.0.1:9/hook"


async def _register(watch_store, **payload) -> Response:
    """One registration POST against the watch door, answered by the response."""
    async with watch_client(watch_store) as client:
        return await client.post("/v1/watches", json=payload)


async def test_registration_admits_the_url_and_schedules_the_first_check(watch_store):
    response = await _register(watch_store, url=TARGET, interval_seconds=INTERVAL, webhook_url=WEBHOOK)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["url"] == TARGET
    assert body["interval_seconds"] == INTERVAL
    assert body["webhook_url"] == WEBHOOK
    assert body["status"] == "active"
    assert body["last_outcome"] is None
    assert body["validators"] == {"etag": None, "last_modified": None}
    # One registration stamps both, so the first check is exactly one interval
    # out -- not one interval plus however long the handler took to answer.
    assert body["next_check_at"] == pytest.approx(body["created_at"] + INTERVAL)


async def test_a_poll_only_watch_registers_with_no_webhook(watch_store):
    response = await _register(watch_store, url=TARGET, interval_seconds=INTERVAL)

    assert response.status_code == 201, response.text
    assert response.json()["webhook_url"] is None


async def test_a_url_this_deployment_will_not_crawl_is_refused(watch_store, monkeypatch):
    block_private_targets(monkeypatch)

    response = await _register(watch_store, url="http://127.0.0.1:9/", interval_seconds=INTERVAL)

    assert response.status_code == 422, response.text
    assert watch_store.list_watches() == []


async def test_a_webhook_this_deployment_cannot_deliver_to_is_refused(watch_store, monkeypatch):
    """Refused at registration, not on the first change: the caller can fix it now."""
    block_private_targets(monkeypatch)

    response = await _register(watch_store, url=TARGET, interval_seconds=INTERVAL, webhook_url=WEBHOOK)

    assert response.status_code == 422, response.text
    assert watch_store.list_watches() == []


async def test_a_non_positive_interval_is_refused(watch_store):
    response = await _register(watch_store, url=TARGET, interval_seconds=0)

    assert response.status_code == 422, response.text


async def test_listing_returns_every_registered_watch(watch_store):
    async with watch_client(watch_store) as client:
        first = await client.post("/v1/watches", json={"url": TARGET, "interval_seconds": INTERVAL})
        await client.post(
            "/v1/watches",
            json={"url": "http://example.com/other", "interval_seconds": 60},
        )

        listing = await client.get("/v1/watches")

    assert listing.status_code == 200, listing.text
    watches = listing.json()["watches"]
    assert len(watches) == 2
    # Registration order, oldest first.
    assert watches[0]["watch_id"] == first.json()["watch_id"]
    assert {watch["url"] for watch in watches} == {TARGET, "http://example.com/other"}


async def test_status_reports_what_the_sweep_recorded(watch_store):
    """The status endpoints read the row the sweep writes, not their own bookkeeping."""
    registered = watch_store.create(canonical(TARGET), interval_seconds=INTERVAL, now=1_000.0)
    watch_store.record_check(
        registered.watch_id,
        outcome=WatchOutcome.NOT_MODIFIED,
        now=2_000.0,
        next_check_at=3_000.0,
        validators=ContentValidators(etag='"v1"'),
    )

    async with watch_client(watch_store) as client:
        response = await client.get(f"/v1/watches/{registered.watch_id}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["last_outcome"] == "not_modified"
    assert body["last_checked_at"] == 2_000.0
    assert body["next_check_at"] == 3_000.0
    assert body["validators"]["etag"] == '"v1"'


async def test_an_unknown_watch_is_not_found(watch_store):
    async with watch_client(watch_store) as client:
        read = await client.get("/v1/watches/nope")
        removed = await client.delete("/v1/watches/nope")

    assert read.status_code == 404, read.text
    assert read.json()["code"] == "not_found"
    assert removed.status_code == 404, removed.text


async def test_deleting_a_watch_stops_it_being_checked(watch_store):
    registered = watch_store.create(
        canonical(TARGET), interval_seconds=INTERVAL, now=0.0, webhook_url=WEBHOOK
    )

    async with watch_client(watch_store) as client:
        removed = await client.delete(f"/v1/watches/{registered.watch_id}")
        read = await client.get(f"/v1/watches/{registered.watch_id}")

    assert removed.status_code == 204, removed.text
    assert read.status_code == 404, read.text
    # The row is gone, so a sweep cannot find a watch nobody is watching.
    assert watch_store.due(10_000.0) == []
