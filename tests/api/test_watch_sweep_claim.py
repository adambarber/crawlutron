"""#528's two contracts: one claim per pass, and a non-blocking delivery.

The claim is a CAS a second sweeper loses; the delivery is a spawned task a
hanging receiver cannot stall the pass behind.
"""

from __future__ import annotations

import asyncio

import pytest

from tests._clocks import arrange_clock
from tests._policy_factories import canonical
from tests._result_builders import engine_result
from tests._watch_harnesses import (
    UNRESTRICTED_HOST_POLICY,
    CrawlClassification,
    CrawlerSettings,
    WatchSweeper,
    WebhookTarget,
    capture_announcements,
    default_sweeper,
    loopback_url,
    watch_sweep,
)

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


def _sweeper_reading(watch_store, clock):
    """A sweeper whose lease renewals read the test's controllable clock."""
    return WatchSweeper(
        watch_store,
        host_policy=UNRESTRICTED_HOST_POLICY,
        settings=CrawlerSettings.from_mapping({}),
        clock=clock,
    )


def _sweeper_on_arranged_time(watch_store, start: float = NOW):
    """A sweeper over an :func:`arrange_clock` the caller advances.
    Returns ``(state, sweeper)``; the caller moves ``state["now"]``."""
    state, clock = arrange_clock(start=start)
    return state, _sweeper_reading(watch_store, clock)


def _slow_then_live(watch_store, etag_server, monkeypatch):
    """Two due rows over distinct URLs, a parked crawl stub, and the events to
    step the pass: returns (slow, watched, inside, releases)."""
    from crawlutron.service import watch_sweep

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


def _start_second_sweeper(watch_store, state):
    """Fast-forward the clock past the claim window and start a second sweeper;
    return its pass task for the caller to await."""
    state["now"] = NOW + 400.0
    second = default_sweeper(watch_store)
    return asyncio.create_task(second.sweep_once(now=NOW + 400.0))


def _start_parked_pass(watch_store, state, monkeypatch, crawl, parked, release):
    """Stub ``crawl``, start the first (clocked) sweeper's pass; return
    ``(pass_one, parked, release)`` for the caller's step-through."""
    monkeypatch.setattr(watch_sweep, "execute_crawl", crawl)
    first = _sweeper_reading(watch_store, lambda: state["now"])
    pass_one = asyncio.create_task(first.sweep_once(now=NOW))
    return pass_one, parked, release


def _parked_crawl_double(parked, release, *, dead_after=False):
    """An execute_crawl double: the FIRST call signals ``parked`` and waits on
    ``release``; later calls answer straight through. ``dead_after`` makes the
    released check fail (the stale-sweeper arrangement)."""
    calls = 0

    async def _crawl(payload, *, settings, shutdown_event, urls=None):
        nonlocal calls
        calls += 1
        if calls > 1:
            return _crawl_envelope(payload.url)
        parked.set()
        await release.wait()
        if dead_after:
            raise ConnectionError("the origin died while the first sweeper waited")
        return _crawl_envelope(payload.url)

    return _crawl


def _registered_webhook_watch(watch_store, etag_server):
    """The suite's standard watch row over the etag server's URL."""
    return watch_store.create(
        canonical(loopback_url(etag_server)),
        webhook_url=_webhook(),
        interval_seconds=INTERVAL,
        now=0.0,
    )


def _delivery_buffer(monkeypatch):
    """A raw deliveries list with the spawn seam stubbed to a capturing task.
    Unlike ``capture_announcements`` the caller parks deliveries itself."""
    deliveries: list[str] = []

    def _spawn(target, payload):
        async def _capture(t, p):
            deliveries.append(str(t))

        return asyncio.create_task(_capture(target, payload))

    monkeypatch.setattr(watch_sweep, "start_webhook_delivery", _spawn)
    return deliveries


async def test_two_concurrent_sweeps_deliver_one_webhook_not_two(watch_store, etag_server, monkeypatch):
    """Two sweeper processes over one due watch must fire the webhook once: the
    second sweeper's claim CAS loses and the row is skipped, not re-delivered."""
    from crawlutron.service import watch_sweep

    watch = _registered_webhook_watch(watch_store, etag_server)
    deliveries = _delivery_buffer(monkeypatch)
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

    monkeypatch.setattr(watch_sweep, "execute_crawl", _parked_crawl)

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


async def _release_and_await(release, pass_one, pass_two=None):
    """Release the parked check, await both passes (second optional), and let a
    spawned delivery land: the step-through tail every parked-pass test shares."""
    release.set()
    out_one = await asyncio.wait_for(pass_one, timeout=10)
    out_two = await asyncio.wait_for(pass_two, timeout=10) if pass_two is not None else None
    await asyncio.sleep(0)
    return out_one, out_two


def _parked_pass_setup(watch_store, monkeypatch, etag_server, *, dead_after=False):
    """The parked-pass arrange: watch row, captured deliveries, clock, events,
    crawl double, and the started pass. Returns the pieces the test steps through."""
    _registered_webhook_watch(watch_store, etag_server)
    deliveries = capture_announcements(monkeypatch)
    state, _ = arrange_clock(start=NOW)
    parked = asyncio.Event()
    release = asyncio.Event()
    crawl = _parked_crawl_double(parked, release, dead_after=dead_after)
    pass_one, _, _ = _start_parked_pass(watch_store, state, monkeypatch, crawl, parked, release)
    return pass_one, parked, release, deliveries, state


async def test_a_check_outliving_the_claim_window_fires_once_not_twice(watch_store, etag_server, monkeypatch):
    """A check that outlives its claim window must still fire once: the sweeper
    re-stamps the lease live before the webhook, so a second sweeper that
    re-claimed the lapsed row mid-check wins the CAS and the first skips."""
    pass_one, parked, release, deliveries, state = _parked_pass_setup(watch_store, monkeypatch, etag_server)
    await asyncio.wait_for(parked.wait(), timeout=10)

    # The claim window lapses while the first sweeper is mid-check.
    pass_two = _start_second_sweeper(watch_store, state)
    await asyncio.wait_for(pass_two, timeout=10)
    await asyncio.sleep(0)

    await _release_and_await(release, pass_one)

    assert [url for url, _ in deliveries] == [WEBHOOK], f"double-fired: {deliveries}"


async def test_a_late_verdict_from_a_sweeper_that_lost_the_row_records_nothing(
    watch_store, etag_server, monkeypatch
):
    """A sweeper that lost its claim mid-check must not record a stale verdict:
    the new owner's fresher record (streak, schedule, validators) stands."""
    pass_one, parked, release, deliveries, state = _parked_pass_setup(
        watch_store, monkeypatch, etag_server, dead_after=True
    )
    await asyncio.wait_for(parked.wait(), timeout=10)

    # Past the claim window the second sweeper re-claims, records its fresher
    # rendered verdict (streak 0, next = NOW+400+INTERVAL), and fires once.
    pass_two = _start_second_sweeper(watch_store, state)
    await asyncio.wait_for(pass_two, timeout=10)

    await _release_and_await(release, pass_one)

    reloaded = watch_store.get(watch_store.list_watches()[0].watch_id)
    assert [url for url, _ in deliveries] == [WEBHOOK]
    assert reloaded.last_outcome == CrawlClassification.rendered
    assert reloaded.consecutive_failures == 0
    assert reloaded.next_check_at == NOW + 400.0 + INTERVAL
    assert reloaded.validators.etag == "v1"


async def test_a_row_in_webhook_delivery_is_unclaimable_until_its_reschedule_matures(
    watch_store, etag_server, monkeypatch
):
    """Delivery atomicity rides the reschedule: once record_check pushes the row
    a full interval out, a second sweep mid-delivery finds nothing due to claim,
    so the in-flight delivery cannot be double-fired."""
    _registered_webhook_watch(watch_store, etag_server)
    delivery_started = asyncio.Event()
    release = asyncio.Event()

    async def _parked(target, payload):
        deliveries.append(str(target))
        delivery_started.set()
        await release.wait()

    def _spawn_parked(target, payload):
        return asyncio.create_task(_parked(target, payload))

    async def _always_rendered(payload, *, settings, shutdown_event, urls=None):
        return _crawl_envelope(payload.url)

    deliveries = _delivery_buffer(monkeypatch)
    monkeypatch.setattr(watch_sweep, "start_webhook_delivery", _spawn_parked)
    monkeypatch.setattr(watch_sweep, "execute_crawl", _always_rendered)

    first = _sweeper_reading(watch_store, lambda: NOW)
    pass_one = asyncio.create_task(first.sweep_once(now=NOW))
    await asyncio.wait_for(delivery_started.wait(), timeout=10)

    second = default_sweeper(watch_store)
    # Past the claim window (300s) but inside the reschedule (3600s): nothing is
    # due with the reschedule intact; with it dropped this sweep re-fires mid-delivery.
    pass_two = asyncio.create_task(second.sweep_once(now=NOW + 400.0))
    outcomes_two = await asyncio.wait_for(pass_two, timeout=10)

    await _release_and_await(release, pass_one)

    assert outcomes_two == [], f"the second sweep re-claimed a row mid-delivery: {outcomes_two}"
    assert deliveries == [WEBHOOK], f"double-fired: {deliveries}"


@pytest.mark.parametrize(
    "claim_age,reclaimable",
    [
        (299.0, False),  # 1s under the window: still held
        (300.0, False),  # exactly at the window: still held (strict <)
        (301.0, True),  # 1s over: stale, re-claimable
    ],
)
async def test_claim_window_staleness_boundary(watch_store, claim_age, reclaimable):
    """A claim is stale-claimable only once its window has fully elapsed:
    exactly at the window the row is still held, one second over it is not.
    Pins claim_due's strict ``claimed_at < now - claim_seconds`` boundary."""
    watch_store.create(canonical("http://127.0.0.1/watch"), interval_seconds=1, now=0.0)
    await watch_store.aclaim_due(NOW, "sweeper-a", claim_seconds=300.0)
    reclaimed = await watch_store.aclaim_due(NOW + claim_age, "sweeper-b", claim_seconds=300.0)
    assert (len(reclaimed) == 1) is reclaimable


def _start_pass(clock, watch_store):
    """A clocked first sweeper's pass task, started at the suite's NOW."""
    first = _sweeper_reading(watch_store, clock)
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
    state, _ = arrange_clock(start=NOW)
    slipped = asyncio.Event()

    def _clock():
        if slipped.is_set():
            state["now"] = NOW + 350.0
        return state["now"]

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
    state, _ = arrange_clock(start=NOW)

    pass_one = _start_pass(lambda: state["now"], watch_store)
    await asyncio.wait_for(inside[slow.url].wait(), timeout=10)

    # Past the claim window, the second sweeper re-claims BOTH rows and works
    # the one sorted after the parked check the first pass is stuck on.
    pass_two = _start_second_sweeper(watch_store, state)
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


async def test_the_forever_loop_forwards_its_clock_to_the_sweeper(watch_store, etag_server, monkeypatch):
    """A clock injected through ``sweep_watches_forever`` must drive lease
    renewals too: renewals stamped from wall time while the pass reads the
    injected clock would mis-age every claim the loop touches."""
    from crawlutron.service import watch_sweep

    watch = watch_store.create(canonical(loopback_url(etag_server)), interval_seconds=INTERVAL, now=0.0)
    inside = asyncio.Event()
    release = asyncio.Event()
    injected = NOW + 1.0  # epoch-2023 stamp; the live wall clock is years later

    async def _parked_crawl(payload, *, settings, shutdown_event, urls=None):
        inside.set()
        await release.wait()
        return _crawl_envelope(payload.url)

    monkeypatch.setattr(watch_sweep, "execute_crawl", _parked_crawl)
    stop = asyncio.Event()
    loop = asyncio.create_task(
        watch_sweep.sweep_watches_forever(
            watch_store,
            host_policy=UNRESTRICTED_HOST_POLICY,
            settings=CrawlerSettings.from_mapping({}),
            shutdown_event=stop,
            interval_seconds=0.0,
            clock=lambda: injected,
        )
    )
    try:
        await asyncio.wait_for(inside.wait(), timeout=10)
        stamp = watch_store._conn.execute(
            "SELECT claimed_at FROM watches WHERE watch_id = ?", (watch.watch_id,)
        ).fetchone()["claimed_at"]
    finally:
        release.set()
        stop.set()
        await asyncio.wait_for(loop, timeout=10)

    assert stamp == injected, f"the loop's renewal read a foreign clock: claimed_at={stamp}"


async def test_a_hanging_webhook_does_not_stall_the_sweep_pass(watch_store, etag_server, monkeypatch):
    """A webhook that never answers must not hold the sweep pass: delivery is a
    tracked background task, so sweep_once returns while it runs (#528)."""
    from crawlutron.service import watch_sweep
    from crawlutron.service.webhooks import drain_webhook_deliveries

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
