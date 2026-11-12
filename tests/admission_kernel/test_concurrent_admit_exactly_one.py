"""B1: concurrent admits never push the gate's in-flight count above the ceiling.

The decide and commit phases must be one atomic lock hold; a split over-commits."""

from __future__ import annotations

import asyncio
import dataclasses
import sys
import threading

from admission import LaneId
from tests.admission_kernel._doors import _GENERAL_ONE
from tests.admission_kernel._gate_factory import GateSpec, build_gate

_THREADS = 8
_ROUNDS = 250


def _race_peak(gate) -> int:
    """Race several admit threads; return the peak real in-flight GENERAL count."""
    switch = sys.getswitchinterval()
    sys.setswitchinterval(1e-5)  # a tiny switch interval widens the decide window
    stop = threading.Event()
    barrier = threading.Barrier(_THREADS)
    peak = 0
    probe_lock = threading.Lock()

    def _monitor() -> None:
        nonlocal peak
        while not stop.is_set():
            with probe_lock:
                peak = max(peak, gate._in_flight[LaneId.GENERAL])

    monitor = threading.Thread(target=_monitor, daemon=True)

    async def _once() -> None:
        for _ in range(_ROUNDS):
            cm = gate.admit(_GENERAL_ONE)
            try:
                granted = await asyncio.wait_for(cm.__aenter__(), timeout=0.5)
            except (TimeoutError, asyncio.CancelledError, RuntimeError):
                continue  # the admit parked; an earlier holder released the slot
            granted.lease.release(work_completed=True)
            await cm.__aexit__(None, None, None)

    def _worker() -> None:
        barrier.wait()
        asyncio.run(_once())

    try:
        monitor.start()
        threads = [threading.Thread(target=_worker) for _ in range(_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30.0)
    finally:
        stop.set()
        sys.setswitchinterval(switch)
    return peak


def test_two_concurrent_admits_capacity_one_exactly_one_grants() -> None:
    """The in-flight count never crosses the ceiling under racing concurrent admits."""
    gate, _ = build_gate(dataclasses.replace(GateSpec(), general_ceiling=1))
    peak = _race_peak(gate)
    # Mutant (two-lock decide/commit): both decides see in_flight=0 before either
    # commits and both commit -- in_flight spikes above the ceiling -> red.
    assert peak <= 1, f"over-committed ceiling-1: peak {peak}"
