"""Tests for two-tier weighted batch admission (replaces the stop-the-world pause).

HTTP tasks are cheap (small RAM); browser tasks are expensive (the browser pool
holds most of the cgroup RAM). :class:`WeightedAdmission` grants as many HTTP
tasks as fit the RAM/CPU headroom, then caps browser tasks at the pool page
capacity, and yields briefly (never parks the whole crawl) when saturated.
"""

import asyncio

import pytest

from app.resource_pressure import (
    AdmissionPolicy,
    WeightedAdmission,
    _build_admission_policy,
    _HTTP_TASK_RAM_BYTES,
    _BROWSER_TASK_RAM_BYTES,
)


class _FakeGate:
    def __init__(self, mem=0.0, cpu=0.0):
        self._mem = mem
        self._cpu = cpu

    def memory_ratio(self):
        return self._mem

    def cpu_ratio(self):
        return self._cpu


def _policy(limit=3 * 1024 * 1024 * 1024, pool_size=2, max_concurrent_pages=3):
    return _build_admission_policy(
        limit, pool_size=pool_size, max_concurrent_pages=max_concurrent_pages
    )


class TestPolicyDerivation:
    def test_browser_pool_reserve_subtracted_from_limit(self):
        # 3 GiB limit, 2 browsers * 700 MiB reserve -> HTTP ceiling from remainder.
        p = _policy()
        assert p.browser_pool_reserve_bytes == 2 * _BROWSER_TASK_RAM_BYTES
        free = (3 * 1024 * 1024 * 1024) - p.browser_pool_reserve_bytes
        assert p.max_http_concurrency == max(1, free // _HTTP_TASK_RAM_BYTES)
        # browser tier is capped at pool_size * max_concurrent_pages
        assert p.max_browser_concurrency == 2 * 3

    def test_http_ceiling_capped_at_hard_max(self):
        huge = _build_admission_policy(
            1024 * 1024 * 1024 * 1024, pool_size=2, max_concurrent_pages=3
        )
        assert huge.max_http_concurrency <= 512

    def test_smaller_limit_yields_smaller_http_ceiling(self):
        small = _policy(limit=1024 * 1024 * 1024)  # 1 GiB
        big = _policy(limit=3 * 1024 * 1024 * 1024)
        assert small.max_http_concurrency < big.max_http_concurrency


class TestWeightedAdmission:
    def test_headroom_from_most_contended_resource(self):
        gate = _FakeGate(mem=0.3, cpu=0.8)
        a = WeightedAdmission(gate, _policy())
        # min(1-0.3, 1-0.8) = 0.2
        assert a.headroom_fraction() == pytest.approx(0.2)

    def test_http_capacity_scales_with_headroom(self):
        gate = _FakeGate(mem=0.0, cpu=0.0)
        a = WeightedAdmission(gate, _policy())
        assert a.http_capacity() == a._p.max_http_concurrency
        gate2 = _FakeGate(mem=0.5, cpu=0.5)
        a2 = WeightedAdmission(gate2, _policy())
        assert a2.http_capacity() == pytest.approx(a._p.max_http_concurrency * 0.5, rel=0.01)

    async def test_admit_grants_all_when_headroom_full(self):
        a = WeightedAdmission(_FakeGate(mem=0.0, cpu=0.0), _policy())
        h, b = await a.admit(want_http=50, want_browser=2)
        assert h == 50
        assert b == 2

    async def test_admit_caps_browser_tier(self):
        # Want more browsers than the tier allows; HTTP still granted.
        a = WeightedAdmission(_FakeGate(mem=0.0, cpu=0.0), _policy())
        h, b = await a.admit(want_http=10, want_browser=99)
        assert b == a.browser_capacity()  # capped
        assert h == 10

    async def test_admit_shrinks_http_under_memory_pressure(self):
        a = WeightedAdmission(_FakeGate(mem=0.9, cpu=0.0), _policy())
        h, b = await a.admit(want_http=200, want_browser=0)
        # headroom 0.1 -> http capacity ~10% of ceiling
        assert h < 200
        assert h > 0

    async def test_admit_yields_briefly_when_fully_saturated(self):
        # Saturated (headroom 0) with demand -> returns (0,0) after a short yield
        # instead of parking the whole crawl indefinitely.
        a = WeightedAdmission(_FakeGate(mem=1.0, cpu=1.0), _policy())
        t0 = asyncio.get_event_loop().time()
        h, b = await a.admit(want_http=5, want_browser=1)
        elapsed = asyncio.get_event_loop().time() - t0
        assert (h, b) == (0, 0)
        assert 0 < elapsed < 1.0  # yielded, did not block forever
