"""Admission test doubles shared by the capacity, scaling and policy gates.

Declared support surface (#476): moved verbatim from
test_admission.py / test_admission_scaling.py / test_admission_policy.py so a
sibling test module imports these from here, not from another test's internals.
"""

from __future__ import annotations

import asyncio

from crawlutron.orchestration import request_pipeline
from crawlutron.runtime.pressure_memory import MemoryStats
from crawlutron.runtime.pressure_sizing import ResourceGate

__all__ = ["PinnedGate", "pinned_resource_gate", "no_crawl_delay", "page_route", "pin_small_box"]


async def no_crawl_delay(url: str) -> float | None:
    """A host that states no Crawl-delay, so a scenario about capacity can pass one."""
    return None


def page_route(url, sent_headers, timeout, max_content_length):
    """A minimal 200 HTML route for a fetch double driving a sync batch."""
    return {
        "status": 200,
        "body": b"<html><body>hello world, enough text to render</body></html>",
        "ctype": "text/html; charset=utf-8",
    }


def pin_small_box(monkeypatch) -> None:
    """Derive a tiny RAM ceiling so the admission gate clearly binds the batch."""
    monkeypatch.setattr(
        request_pipeline,
        "read_memory_stats",
        lambda: MemoryStats(total_bytes=1 << 30, used_bytes=0),
    )
    monkeypatch.setattr(request_pipeline, "derive_browser_pool_size", lambda: 2)


def pinned_resource_gate(monkeypatch, *, cpu_ratio, memory_ratio, max_wait_seconds=0.05):
    """A ResourceGate with both sampler ratios pinned, an injected clock, and recorded waits.

    The recorded sleep advances the clock by the interval it was asked to wait,
    so the wait ends on the gate's OWN bound rather than a real one.
    """
    now = [0.0]
    gate = ResourceGate(
        clock=lambda: now[0],
        cpu_ratio=lambda: cpu_ratio,
        memory_ratio=lambda: memory_ratio,
    )
    gate.memory.max_wait_seconds = max_wait_seconds
    gate.cpu.max_wait_seconds = max_wait_seconds
    waited = []
    yield_to_loop = asyncio.sleep

    async def recording_sleep(seconds):
        waited.append(seconds)
        now[0] += seconds
        await yield_to_loop(0)

    monkeypatch.setattr(asyncio, "sleep", recording_sleep)
    return gate, waited


class PinnedGate(ResourceGate):
    """A ResourceGate with fixed scales and ratios, recording consultations.

    It subclasses the production type so dispatch is compiled against the same
    boundary, and skips ``__init__`` to avoid live gates reading the host cgroup.
    """

    def __init__(self, *, batch=1.0, http=1.0, mem=0.0, cpu=0.0):
        self.batch = batch
        self.http = http
        self._mem = mem
        self._cpu = cpu
        self.calls: list[str] = []

    async def wait_while_saturated(self, max_wait_seconds: float | None = None):
        # The parameter is part of the signature this double stands in for. Dropping it would let a caller
        # passing a budget through be type-checked against a gate that silently cannot receive one.
        self.calls.append("wait")

    def batch_scale_factor(self):
        self.calls.append("batch")
        return self.batch

    def http_scale_factor(self):
        self.calls.append("http")
        return self.http

    def memory_ratio(self):
        return self._mem

    def cpu_ratio(self):
        return self._cpu
