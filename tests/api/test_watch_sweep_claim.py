"""#528's two contracts: one claim per pass, and a non-blocking delivery.

The claim is a CAS a second sweeper loses; the delivery is a spawned task a
hanging receiver cannot stall the pass behind.
"""

from __future__ import annotations

import asyncio

from crawlutron.api.watch_sweep import WatchSweeper
from crawlutron.config.settings import CrawlerSettings
from crawlutron.models.webhook_target import WebhookTarget
from tests._policy_factories import UNRESTRICTED_HOST_POLICY, canonical
from tests._result_builders import engine_result

NOW = 1_700_000_000.0
INTERVAL = 3600
WEBHOOK = "http://watcher.example.com/hook"


def _webhook() -> WebhookTarget:
    return WebhookTarget.admit(WEBHOOK, host_policy=UNRESTRICTED_HOST_POLICY)


def origin_url(server) -> str:
    """The loopback URL of a served fixture."""
    return f"http://127.0.0.1:{server.server_port}/"


def _sweeper(watch_store) -> WatchSweeper:
    return WatchSweeper(
        watch_store, host_policy=UNRESTRICTED_HOST_POLICY, settings=CrawlerSettings.from_mapping()
    )


async def test_two_concurrent_sweeps_deliver_one_webhook_not_two(watch_store, etag_server, monkeypatch):
    """Two sweeper processes over one due watch must fire the webhook once: the
    second sweeper's claim CAS loses and the row is skipped, not re-delivered."""
    from crawlutron.api import watch_sweep

    watch = watch_store.create(
        canonical(origin_url(etag_server)),
        webhook_url=_webhook(),
        interval_seconds=INTERVAL,
        now=0.0,
    )
    deliveries: list[str] = []
    first_in_check = asyncio.Event()
    release = asyncio.Event()

    def _envelope(url: str) -> dict[str, object]:
        return {
            "request_id": "x",
            "submitted_count": 1,
            "policy_schema_version": "",
            "results": [engine_result(url, "rendered", validators={"etag": "v1"})],
        }

    async def _parked_crawl(payload, *, settings, shutdown_event, urls=None):
        """Park the FIRST sweep inside its check, holding the claim window open;
        the second sweep's own check runs straight through."""
        if first_in_check.is_set():
            return _envelope(payload.url)
        first_in_check.set()
        await release.wait()
        return _envelope(payload.url)

    async def _capture(target, payload):
        deliveries.append(str(target))

    def _spawn(target, payload):
        return asyncio.create_task(_capture(target, payload))

    monkeypatch.setattr(watch_sweep, "execute_crawl", _parked_crawl)
    monkeypatch.setattr(watch_sweep, "start_webhook_delivery", _spawn)

    first, second = _sweeper(watch_store), _sweeper(watch_store)
    pass_one = asyncio.create_task(first.sweep_once(now=NOW))
    # The second sweeper starts while the first holds the watch mid-check.
    await asyncio.wait_for(first_in_check.wait(), timeout=10)
    pass_two = asyncio.create_task(second.sweep_once(now=NOW))
    await asyncio.wait_for(pass_two, timeout=10)
    release.set()
    await asyncio.wait_for(pass_one, timeout=10)

    assert deliveries == [WEBHOOK], f"double-fired: {deliveries}"
    assert watch_store.get(watch.watch_id).next_check_at == NOW + INTERVAL


async def test_a_hanging_webhook_does_not_stall_the_sweep_pass(watch_store, etag_server, monkeypatch):
    """A webhook that never answers must not hold the sweep pass: delivery is a
    tracked background task, so sweep_once returns while it runs (#528)."""
    from crawlutron.api import watch_sweep
    from crawlutron.api.webhooks import drain_webhook_deliveries

    watch = watch_store.create(
        canonical(origin_url(etag_server)),
        webhook_url=_webhook(),
        interval_seconds=INTERVAL,
        now=0.0,
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def _hanging_delivery(target, payload):
        started.set()
        await release.wait()

    def _spawn_hanging(target, payload):
        return asyncio.create_task(_hanging_delivery(target, payload))

    monkeypatch.setattr(watch_sweep, "start_webhook_delivery", _spawn_hanging)

    sweeper = _sweeper(watch_store)
    sweep = asyncio.create_task(sweeper.sweep_once(now=NOW))

    async def finish_sweep():
        """The pass must end while the delivery still hangs (0.2s bound)."""
        await asyncio.wait_for(started.wait(), timeout=5)
        return await asyncio.wait_for(asyncio.shield(sweep), timeout=0.2)

    outcomes = await asyncio.wait_for(finish_sweep(), timeout=6)
    assert outcomes, "the sweep pass did not complete around the hanging delivery"
    release.set()
    await drain_webhook_deliveries()
    assert watch_store.get(watch.watch_id).next_check_at == NOW + INTERVAL
