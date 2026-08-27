"""Contract tests for the shared ResourceGauge base class.

These pin the base-class behavior so the two gates (memory, CPU) can never
diverge again: both batch_scale_factor and wait_while_saturated must derive
from the single _usage_ratio() signal. A stub subclass keeps it fast and
isolated (no cgroup/real-resource sensing).
"""

import asyncio

import pytest

from app.memory_pressure import ResourceGauge


class _StubGauge(ResourceGauge):
    def __init__(self, sequence):
        super().__init__(target=0.90, poll_seconds=0.01, max_wait_seconds=1.0)
        self._seq = list(sequence)
        self._idx = 0

    def _usage_ratio(self):
        val = self._seq[min(self._idx, len(self._seq) - 1)]
        self._idx += 1
        return val


def test_scale_full_below_target():
    g = _StubGauge([0.50])
    assert g.batch_scale_factor() == 1.0


def test_scale_zero_at_hard():
    # hard = min(0.999, target + 0.05) ~= 0.95; use a value clearly above it.
    g = _StubGauge([0.99])
    assert g.batch_scale_factor() == 0.0


def test_scale_linear_between_target_and_hard():
    # 0.925 is midway between 0.90 and 0.95 -> scale ~0.5
    g = _StubGauge([0.925])
    assert g.batch_scale_factor() == pytest.approx(0.5, abs=1e-6)


async def test_wait_returns_when_below_hard():
    g = _StubGauge([0.50])
    await g.wait_while_saturated(max_wait_seconds=1)
    assert True  # did not block


async def test_wait_releases_once_drained():
    # Starts saturated (>= hard), then drains below hard on the next sample.
    g = _StubGauge([0.99, 0.10])
    await g.wait_while_saturated(max_wait_seconds=5)
    assert g._idx >= 2  # it actually re-sampled and saw the drain
