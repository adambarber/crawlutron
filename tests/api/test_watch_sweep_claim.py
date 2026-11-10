"""#528's two contracts: one claim per pass, and a non-blocking delivery.

The claim is a CAS a second sweeper loses; the delivery is a spawned task a
hanging receiver cannot stall the pass behind.
"""

from __future__ import annotations

import asyncio

from crawlutron.api.watch_sweep import WatchSweeper
from crawlutron.config.settings import CrawlerSettings
from crawlutron.models.webhook_target import WebhookTarget
from crawlutron.vocabulary.taxonomy import CrawlClassification
from tests._policy_factories import UNRESTRICTED_HOST_POLICY, canonical
from tests._result_builders import engine_result
from tests._watch_harnesses import capture_announcements, default_sweeper, loopback_url

NOW = 1_700_000_000.0
INTERVAL = 3600
WEBHOOK = "https://watcher.example.com/hook"


def _webhook() -> WebhookTarget:
    return WebhookTarget.admit(WEBHOOK, host_policy=UNRESTRICTED_HOST_POLICY)


def _crawl_envelope(url: str) -> dict[str, object]:
    return {
        "request_id": "x",
        "submitted_count": 1,
        "policy_schema_version": "",
        "results": [engine_result(url, "rendered", validators={"etag": "v1"})],
    }


def _parking_crawl(inside, releases):
    """An execute_crawl double: each URL's first call parks on its release event;
    later calls answer straight through so a reclaiming sweep completes."""
    calls: dict[str, int] = {}

    async def _crawl(payload, *, settings, shutdown_event, urls=None):
        calls[payload.url] = calls.get(payload.url, 0) + 1
        if calls[payload.url] == 1:
            inside[payload.url].set()
            await releases[payload.url].wait()
        return _crawl_envelope(payload.url)

    return _crawl


def _clocked_sweeper(watch_store, clock):
    """A sweeper whose lease renewals read the test's controllable clock."""
    return WatchSweeper(
        watch_store,
        host_policy=UNRESTRICTED_HOST_POLICY,
        settings=CrawlerSettings.from_mapping({}),
        clock=clock,
    )


def _slow_then_live(watch_store, etag_server, monkeypatch):
    """Two due rows over distinct URLs, a parked crawl stub, and the events to
    step the pass: returns (slow, watched, inside, releases)."""
    from crawlutron.api import watch_sweep

    slow = watch_store.create(
        canonical(loopback_url(etag_server) + "slow"), interval_seconds=INTERVAL, now=0.0
    )
    watched = watch_store.create(
        canonical(loopback_url(etag_server) + "live"),
        webhook_url=_webhook(),
        interval_seconds=INTERVAL,
        now=100.0,
    )
    inside = {slow.url: asyncio.Event(), watched.url: asyncio.Event()}
    releases = {slow.url: asyncio.Event(), watched.url: asyncio.Event()}
    monkeypatch.setattr(watch_sweep, "execute_crawl", _parking_crawl(inside, releases))
    return slow, watched, inside, releases


async def test_two_concurrent_sweeps_deliver_one_webhook_not_two(watch_store, etag_server, monkeypatch):
    """Two sweeper processes over one due watch must fire the webhook once: the
    second sweeper's claim CAS loses and the row is skipped, not re-delivered."""
    from crawlutron.api import watch_sweep

    watch = watch_store.create(
        canonical(loopback_url(etag_server)),
        webhook_url=_webhook(),
        interval_seconds=INTERVAL,
        now=0.0,
    )
    deliveries: list[str] = []
    first_in_check = asyncio.Event()
    release = asyncio.Event()

    async def _parked_crawl(payload, *, settings, shutdown_event, urls=None):
        """Park the FIRST sweep inside its check, holding the claim window open;
        the second sweep's own check runs straight through."""
        if first_in_check.is_set():
            return _crawl_envelope(payload.url)
        first_in_check.set()
        await release.wait()
        return _crawl_envelope(payload.url)

    async def _capture(target, payload):
        deliveries.append(str(target))

    def _spawn(target, payload):
        return asyncio.create_task(_capture(target, payload))

    monkeypatch.setattr(watch_sweep, "execute_crawl", _parked_crawl)
    monkeypatch.setattr(watch_sweep, "start_webhook_delivery", _spawn)

    first, second = default_sweeper(watch_store), default_sweeper(watch_store)
    pass_one = asyncio.create_task(first.sweep_once(now=NOW))
    # The second sweeper starts while the first holds the watch mid-check.
    await asyncio.wait_for(first_in_check.wait(), timeout=10)
    pass_two = asyncio.create_task(second.sweep_once(now=NOW))
    await asyncio.wait_for(pass_two, timeout=10)
    release.set()
    await asyncio.wait_for(pass_one, timeout=10)

    assert deliveries == [WEBHOOK], f"double-fired: {deliveries}"
    assert watch_store.get(watch.watch_id).next_check_at == NOW + INTERVAL


def _start_pass(clock, watch_store):
    """A clocked first sweeper's pass task, started at the suite's NOW."""
    first = _clocked_sweeper(watch_store, clock)
    return asyncio.create_task(first.sweep_once(now=NOW))


async def test_a_slow_pass_keeps_its_claim_so_a_late_second_sweeper_skips_the_live_row(
    watch_store, etag_server, monkeypatch
):
    """A pass outliving its claim window must not lose the row it is working:
    each check re-stamps the lease at its start, so a second sweeper arriving
    past the window finds the live row fresh-claimed and checks nothing."""
    slow, watched, inside, releases = _slow_then_live(watch_store, etag_server, monkeypatch)
    deliveries = capture_announcements(monkeypatch)
    # The clock idles at NOW until the slow check parks: the slow row's renewal
    # stamps NOW, the live row's stamps NOW+350; the attacker lands at NOW+400.
    clock_t = {"t": NOW}
    slipped = asyncio.Event()

    def _clock():
        if slipped.is_set():
            clock_t["t"] = NOW + 350.0
        return clock_t["t"]

    pass_one = _start_pass(_clock, watch_store)
    await asyncio.wait_for(inside[slow.url].wait(), timeout=10)
    slipped.set()
    releases[slow.url].set()
    await asyncio.wait_for(inside[watched.url].wait(), timeout=10)

    second = default_sweeper(watch_store)
    pass_two = asyncio.create_task(second.sweep_once(now=NOW + 400.0))
    outcomes_two = await asyncio.wait_for(pass_two, timeout=10)
    releases[watched.url].set()
    outcomes_one = await asyncio.wait_for(pass_one, timeout=10)
    await asyncio.sleep(0)

    # Only the LIVE row is protected: the completed slow row is legitimately
    # stale-claimable. The live row was never re-claimed; the webhook fired once.
    reloaded = watch_store.get(watched.watch_id)
    assert outcomes_two == [], f"the second sweeper re-claimed a live row: {outcomes_two}"
    assert reloaded.next_check_at == NOW + INTERVAL
    assert [url for url, _ in deliveries] == [WEBHOOK], f"double-fired: {deliveries}"
    assert outcomes_one == [CrawlClassification.rendered, CrawlClassification.rendered]


async def test_a_row_reclaimed_by_a_second_sweeper_is_skipped_not_reworked(
    watch_store, etag_server, monkeypatch
):
    """A renewal from a token that no longer owns the row must no-op, and the
    working sweeper must skip the row: reworking it is the double-fire the
    claim exists to prevent, and its verdict must not overwrite the new owner's."""
    slow, watched, inside, releases = _slow_then_live(watch_store, etag_server, monkeypatch)
    deliveries = capture_announcements(monkeypatch)
    clock_t = {"t": NOW}

    pass_one = _start_pass(lambda: clock_t["t"], watch_store)
    await asyncio.wait_for(inside[slow.url].wait(), timeout=10)

    # Past the claim window, the second sweeper re-claims BOTH rows and works
    # the one sorted after the parked check the first pass is stuck on.
    clock_t["t"] = NOW + 400.0
    second = default_sweeper(watch_store)
    pass_two = asyncio.create_task(second.sweep_once(now=NOW + 400.0))
    await asyncio.wait_for(inside[watched.url].wait(), timeout=10)

    releases[slow.url].set()
    outcomes_one = await asyncio.wait_for(pass_one, timeout=10)
    releases[watched.url].set()
    await asyncio.wait_for(pass_two, timeout=10)
    await asyncio.sleep(0)

    # The first sweeper's renewal of the reclaimed row failed, so it checked
    # nothing past its parked check and recorded nothing over the new owner.
    assert len(outcomes_one) == 1, f"the first sweeper reworked a lost row: {outcomes_one}"
    assert [url for url, _ in deliveries] == [WEBHOOK], f"double-fired: {deliveries}"
    reloaded = watch_store.get(watched.watch_id)
    assert reloaded.next_check_at == NOW + 400.0 + INTERVAL


async def test_a_hanging_webhook_does_not_stall_the_sweep_pass(watch_store, etag_server, monkeypatch):
    """A webhook that never answers must not hold the sweep pass: delivery is a
    tracked background task, so sweep_once returns while it runs (#528)."""
    from crawlutron.api import watch_sweep
    from crawlutron.api.webhooks import drain_webhook_deliveries

    watch = watch_store.create(
        canonical(loopback_url(etag_server)),
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

    sweeper = default_sweeper(watch_store)
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
