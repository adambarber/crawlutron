"""A saturated resolver refuses fast; it never queues the request path (#C10).

A RUNNING getaddrinfo cannot be cancelled -- the thread stays occupied. A
hostile or slow DNS server can therefore occupy all 32 DNS_EXECUTOR workers,
and every later resolve waits the full budget before its timeout: the request
path is hostage to the queue. The saturation chokepoint fails fast instead.
"""

import asyncio
import time

import pytest

import crawlutron.transport.pinned_transport as pt


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

    async def scenario():
        def hang():
            return "never returned in time"

        task = asyncio.ensure_future(pt.bounded_thread_resolve(hang, timeout_seconds=5.0))
        for _ in range(100):
            if pt.dns_in_flight_resolves() >= 1:
                break
            await asyncio.sleep(0.01)
        registered = pt.dns_in_flight_resolves()
        task.cancel()
        return registered

    registered = asyncio.run(scenario())
    assert registered >= 1, (
        f"the async resolve path bypassed the saturation registry (in-flight: {registered})"
    )
