"""A saturated resolver refuses fast; it never queues the request path. A RUNNING getaddrinfo cannot be
cancelled -- the thread stays occupied.
"""

import asyncio
import threading
import time

import pytest

import crawlutron.transport.pinned_dns as pt
from tests._fetcher_factories import ParkedResolver
from tests._timing_harnesses import await_until


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
    assert pt.dns_in_flight_resolves() == 0, "this test leaked registered DNS futures"


def test_a_saturated_resolver_fails_fast(monkeypatch):
    """When every resolver worker is inside a live resolve, a new resolve must
    raise the classifiable ResolverBudgetExceeded in ~no time, NOT queue the
    full budget behind uncancellable threads."""
    parked = ParkedResolver(monkeypatch)
    parked.occupy(pt, pt.DNS_WORKERS)
    # The refusal is a pre-submit decision, so it is a fail-fast property:
    # run it under a hang guard, not a stopwatch.
    with pytest.raises(pt.ResolverBudgetExceeded, match="saturated"):
        await_guarded(lambda: pt.bounded_addrinfo("victim.test"))
    parked.release(pt)


def await_guarded(work, ceiling_seconds=1.0):
    """Run a synchronous call on a thread under a tight purpose-bound ceiling:
    the saturated refusal must return well inside it; a queued call fails the
    is_alive check as a hang, never as merely slow."""
    outcome = {}

    def runner():
        try:
            outcome["value"] = work()
        except BaseException as exc:  # re-raised in the caller below
            outcome["error"] = exc

    worker = threading.Thread(target=runner, daemon=True)
    worker.start()
    worker.join(timeout=ceiling_seconds)
    assert not worker.is_alive(), "the saturated refusal did not return fast -- it queued"
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("value")


def test_the_accounting_door_reports_live_resolves():
    """Below saturation the resolve path is unchanged, and the chokepoint's own
    accounting starts at zero: no in-flight resolve, no saturation verdict."""
    assert pt.dns_in_flight_resolves() == 0


def _parked_scenario():
    """Shared arrange of the parked-worker tests: the events, the parked
    resolve body wired to them, and the submit-and-register task opener."""
    started = threading.Event()
    hold = threading.Event()
    worker_returned = threading.Event()

    def parked():
        return _parked_worker(started, hold, worker_returned)

    async def submitted(timeout_seconds):
        return asyncio.ensure_future(pt.bounded_thread_resolve(parked, timeout_seconds=timeout_seconds))

    return started, hold, worker_returned, submitted


def test_the_async_path_is_registered_too():
    """bounded_thread_resolve is the ASYNC DNS entry point -- its futures must register in the same
    saturation registry, or the chokepoint's guarantee holds for only one of two paths (adversarial pass
    5, confirmed defect).
    """
    started, hold, worker_returned, submitted = _parked_scenario()

    async def scenario():
        task = await submitted(5.0)
        await _await_registration(100)
        registered = pt.dns_in_flight_resolves()
        # A pre-start cancel skips the queued work item entirely, so cancel
        # only once the worker is provably inside parked() (hang-safe bound).
        await await_until(started.is_set, "the parked worker to start")
        task.cancel()
        hold.set()
        return registered

    registered = asyncio.run(scenario())
    assert registered >= 1, (
        f"the async resolve path bypassed the saturation registry (in-flight: {registered})"
    )
    _drain_parked_worker(worker_returned)


def _drain_parked_worker(worker_returned: threading.Event) -> None:
    """Shared tail of the parked-worker tests: the worker's own last act gates
    the drain; the poll closes only the return-to-callback window."""
    assert worker_returned.wait(10.0), "the parked worker never completed"
    _join_via_polling()
    assert pt.dns_in_flight_resolves() == 0, "a completed worker stayed registered"


def _parked_worker(started: threading.Event, hold: threading.Event, worker_returned: threading.Event) -> str:
    """A resolve body that signals started, parks on hold, signals returned."""
    started.set()
    hold.wait(timeout=10.0)
    worker_returned.set()
    return "parked"


async def _await_registration(poll_limit: int) -> None:
    """Yield the loop until the resolve registers, bounded and hang-safe."""
    for _ in range(poll_limit):
        if pt.dns_in_flight_resolves() >= 1:
            break
        await asyncio.sleep(0.01)


def _join_via_polling(seconds: float = 5.0) -> None:
    """Hang-guard join: poll the registry until drained, never a sleep loop."""
    deadline = time.monotonic() + seconds
    while pt.dns_in_flight_resolves() and time.monotonic() < deadline:
        time.sleep(0.01)


@pytest.mark.real_sleeps
def test_a_timed_out_wait_still_counts_the_worker(monkeypatch):
    """The registry must hold a future until the RESOLVER THREAD completes, not until the caller's wait times
    out: a getaddrinfo still running after the wait expires is exactly the blind window the saturation law
    exists to close. Released via Events, no wall-clock sleeps.
    """
    started, hold, worker_returned, submitted = _parked_scenario()

    async def scenario():
        task = await submitted(0.1)
        # The worker parks on ``hold``; ``started`` only proves it began, so the
        # loop stays free here while the registration becomes visible.
        await _await_registration(200)
        assert pt.dns_in_flight_resolves() >= 1, "the parked resolve never registered"
        occupied = ParkedResolver(monkeypatch)
        occupied.occupy(pt, pt.DNS_WORKERS - 1)
        # occupy() waits for >= DNS_WORKERS - 1 futures, but that count includes
        # the 1 pre-registered parked worker; wait for full saturation instead.
        deadline = time.monotonic() + 5.0
        while pt.dns_in_flight_resolves() < pt.DNS_WORKERS and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pt.dns_in_flight_resolves() >= pt.DNS_WORKERS, (
            "resolver not fully saturated before the refusal probe"
        )
        try:
            with pytest.raises(pt.ResolverBudgetExceeded, match="budget"):
                await task  # the caller's wait expires; the WORKER is still parked
            assert started.is_set(), (
                "pickup lost the race with the 0.1s budget: the parked-worker premise is unestablished"
            )
            with pytest.raises(pt.ResolverBudgetExceeded, match="saturated"):
                # Wait expired, thread parked: with the parked worker counted the
                # pool is full and a fresh resolve must refuse fast.
                pt.bounded_addrinfo("victim.test")
            hold.set()
        finally:
            occupied.release(pt)

    asyncio.run(scenario())
    _drain_parked_worker(worker_returned)
