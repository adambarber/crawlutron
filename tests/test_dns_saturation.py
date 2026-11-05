"""A saturated resolver refuses fast; it never queues the request path (#C10).

A RUNNING getaddrinfo cannot be cancelled -- the thread stays occupied. A
hostile or slow DNS server can therefore occupy all 32 DNS_EXECUTOR workers,
and every later resolve waits the full budget before its timeout: the request
path is hostage to the queue. The saturation chokepoint fails fast instead.
"""

import asyncio
import threading
import time

import pytest

import crawlutron.transport.pinned_dns as pt


@pytest.fixture(autouse=True)
def _hermetic_registry():
    """The saturation verdicts must not read pool state other tests left
    behind: drain the shared registry first, and name the contamination
    instead of letting a leaked future decide a later assertion."""
    deadline = time.monotonic() + 15.0
    while pt.dns_in_flight_resolves() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert pt.dns_in_flight_resolves() == 0, "a prior test leaked registered DNS futures"
    yield


def test_a_saturated_resolver_fails_fast():
    """When every resolver worker is inside a live resolve, a new resolve must
    raise the classifiable ResolverBudgetExceeded in ~no time, NOT queue the
    full budget behind uncancellable threads. Seeded through the module's own
    test door -- the shared executor may carry lingering threads from earlier
    tests, so the verdict is proven at its decision input."""
    pt.dns_seed_saturation(32)
    try:
        assert pt.dns_in_flight_resolves() >= 32
        started = time.monotonic()
        with pytest.raises(pt.ResolverBudgetExceeded, match="saturated"):
            pt.bounded_addrinfo("victim.test")
        elapsed = time.monotonic() - started
        assert elapsed < 1.0, f"the fast-fail took {elapsed:.2f}s -- it queued instead"
    finally:
        pt.dns_clear_saturation()


def test_the_accounting_door_reports_live_resolves():
    """Below saturation the resolve path is unchanged, and the chokepoint's own
    accounting starts at zero: no in-flight resolve, no saturation verdict."""
    assert pt.dns_in_flight_resolves() == 0


def test_the_async_path_is_registered_too():
    """bounded_thread_resolve is the ASYNC DNS entry point (browser targets,
    search providers) -- its futures must register in the same saturation
    registry, or the chokepoint's guarantee holds for only one of two paths
    (adversarial pass 5, confirmed defect)."""
    hold = threading.Event()
    worker_returned = threading.Event()

    def parked():
        hold.wait(timeout=10.0)
        worker_returned.set()
        return "never returned in time"

    async def scenario():
        task = asyncio.ensure_future(pt.bounded_thread_resolve(parked, timeout_seconds=5.0))
        for _ in range(100):
            if pt.dns_in_flight_resolves() >= 1:
                break
            await asyncio.sleep(0.01)
        registered = pt.dns_in_flight_resolves()
        task.cancel()
        hold.set()
        return registered

    registered = asyncio.run(scenario())
    assert registered >= 1, (
        f"the async resolve path bypassed the saturation registry (in-flight: {registered})"
    )
    # The discard runs in the completing worker thread: the worker's own last
    # act gates the drain; the poll closes only the return-to-callback window.
    assert worker_returned.wait(10.0), "the parked worker never completed"
    _join_via_polling()
    assert pt.dns_in_flight_resolves() == 0, "a completed worker stayed registered"


def _join_via_polling(seconds: float = 5.0) -> None:
    """Hang-guard join: poll the registry until drained, never a sleep loop."""
    deadline = time.monotonic() + seconds
    while pt.dns_in_flight_resolves() and time.monotonic() < deadline:
        time.sleep(0.01)


def test_a_timed_out_wait_still_counts_the_worker():
    """The registry must hold a future until the RESOLVER THREAD completes, not
    until the caller's wait times out: a getaddrinfo still running inside its
    worker after the wait expires is exactly the blind window the saturation
    law exists to close (wave-3 N1). Released via Events, no wall-clock sleeps."""
    started = threading.Event()
    hold = threading.Event()
    worker_returned = threading.Event()

    def parked():
        started.set()
        hold.wait(timeout=10.0)
        worker_returned.set()
        return "done"

    async def scenario():
        task = asyncio.ensure_future(pt.bounded_thread_resolve(parked, timeout_seconds=0.1))
        # The worker parks on ``hold``; ``started`` only proves it began, so the
        # loop stays free here while the registration becomes visible.
        for _ in range(200):
            if pt.dns_in_flight_resolves() >= 1:
                break
            await asyncio.sleep(0.01)
        assert pt.dns_in_flight_resolves() >= 1, "the parked resolve never registered"
        pt.dns_seed_saturation(pt.DNS_WORKERS - 1)
        try:
            with pytest.raises(pt.ResolverBudgetExceeded, match="budget"):
                await task  # the caller's wait expires; the WORKER is still parked
            with pytest.raises(pt.ResolverBudgetExceeded, match="saturated"):
                # Wait expired, thread parked: with the parked worker counted the
                # pool is full and a fresh resolve must refuse fast.
                pt.bounded_addrinfo("victim.test")
            hold.set()
        finally:
            pt.dns_clear_saturation()

    asyncio.run(scenario())
    # The discard runs in the completing worker thread: the worker's own last
    # act gates the drain; the poll closes only the return-to-callback window.
    assert worker_returned.wait(10.0), "the parked worker never completed"
    _join_via_polling()
    assert pt.dns_in_flight_resolves() == 0, "a completed worker stayed registered"
