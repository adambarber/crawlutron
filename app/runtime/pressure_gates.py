"""CPU and combined resource gates over probe readings."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any, cast

from app.runtime.pressure_memory import MemoryGate, ResourceGauge
from app.runtime.pressure_probes import (
    DEFAULT_CPU_BUDGET,
    DEFAULT_CPU_HARD,
    DEFAULT_CPU_TARGET,
    LoopLagProbe,
    read_cpu_quota,
    read_cpu_usage_seconds,
)

# --- CPU admission gate (rate-based) ---


class CpuGate(ResourceGauge):
    """Admission gate on CPU utilization relative to the cgroup quota -- a rate.

    Below ``target`` scale is 1.0, between target and hard it scales down linearly,
    at/above hard it waits; an unbounded cgroup with no ``cpu_budget`` is None.
    """

    _default_target = DEFAULT_CPU_TARGET
    _default_hard = DEFAULT_CPU_HARD

    def __init__(
        self,
        target: float | None = None,
        *,
        sample_seconds: float = 0.25,
        min_window_seconds: float = 0.05,
        cpu_budget: float | None = None,
        **kwargs: object,
    ):
        # Validated boundary: CpuGate only adds sample knobs; any other kwarg is
        # a ResourceGauge parameter the base validates itself.
        super().__init__(target=target, **cast("dict[str, Any]", kwargs))
        self.sample_seconds = sample_seconds
        self.min_window_seconds = min_window_seconds
        self._cpu_budget = cpu_budget if cpu_budget is not None else DEFAULT_CPU_BUDGET
        self._last_usage: float | None = None
        self._last_wall: float | None = None
        self._ratio: float = 0.0
        self._quota: float | None = None
        self._quota_loaded: bool = False

    def _quota_cpus(self) -> float | None:
        """Allocated CPUs: the cgroup quota, or a manual budget if unbounded."""
        if not self._quota_loaded:
            self._quota = read_cpu_quota(self.cgroup_root)
            self._quota_loaded = True
        if self._quota is None:
            return self._cpu_budget
        return self._quota

    def _sample(self) -> float | None:
        """Current utilization fraction vs quota, or None if unbounded.

        Updates the rolling sample only when at least ``min_window_seconds`` of
        wall time has elapsed, so back-to-back calls do not produce a noisy rate.
        """
        quota = self._quota_cpus()
        if quota is None or quota <= 0:
            return None
        usage = read_cpu_usage_seconds(self.cgroup_root)
        now = time.monotonic()
        if usage is None:
            return None
        if self._last_usage is None or self._last_wall is None:
            self._last_usage = usage
            self._last_wall = now
            return self._ratio  # 0.0 on first sight
        dt = now - self._last_wall
        if dt < self.min_window_seconds:
            return self._ratio
        du = usage - self._last_usage
        instant = (du / dt) / quota if dt > 0 else self._ratio
        # Exponential smoothing absorbs scheduler bursts without erasing signal.
        self._ratio = instant if self._ratio == 0.0 else self._ratio * 0.5 + instant * 0.5
        self._last_usage = usage
        self._last_wall = now
        return self._ratio

    def _read_usage(self) -> float:
        r = self._sample()
        return 0.0 if r is None else r

    def cpu_ratio(self) -> float:
        """Latest utilization fraction vs quota (0.0 when unbounded)."""
        return self.usage_ratio()

    def is_warm(self) -> bool:
        """Whether the gate has a real utilization sample yet.

        True when the cgroup is CPU-unbounded or one rate sample was taken; False only
        on a cold start with a real ceiling, when the first batch should ramp.
        """
        if self._quota_cpus() is None:
            return True
        return self._last_usage is not None and self._last_wall is not None


# --- Combined gate over memory + CPU ---


class ResourceGate:
    """Combined admission gate over memory + CPU, the engine's single gate.

    Dispatchers wait while *either* resource is saturated; the batch scale factor
    is the *minimum* across resources, so the most-contended one sets the pace.
    """

    def __init__(
        self,
        *,
        target_mem: float | None = None,
        target_cpu: float | None = None,
        cgroup_root: str = "/sys/fs/cgroup",
        poll_seconds: float = 0.1,
        max_wait_seconds: float = 30.0,
        sample_seconds: float = 0.25,
        cpu_budget: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        memory_ratio: Callable[[], float] | None = None,
        cpu_ratio: Callable[[], float] | None = None,
        lag_probe_enabled: bool = False,
        lag_probe_clock: Callable[[], float] | None = None,
        lag_probe_sleep: Callable[[float], Awaitable[None]] | None = None,
    ):
        self.memory = MemoryGate(
            target=target_mem,
            poll_seconds=poll_seconds,
            max_wait_seconds=max_wait_seconds,
            cgroup_root=cgroup_root,
            clock=clock,
            usage_ratio=memory_ratio,
        )
        self.cpu = CpuGate(
            target=target_cpu,
            poll_seconds=poll_seconds,
            max_wait_seconds=max_wait_seconds,
            cgroup_root=cgroup_root,
            sample_seconds=sample_seconds,
            cpu_budget=cpu_budget,
            usage_ratio=cpu_ratio,
        )
        self._poll_seconds = poll_seconds
        # Opt-in throttle signal (module probe above); injected, not global, so
        # a test can enable it without rebinding a name the whole tree reads.
        self._lag_probe = LoopLagProbe(
            clock=lag_probe_clock or time.monotonic,
            sleep=lag_probe_sleep or asyncio.sleep,
        )
        self._lag_probe_enabled = lag_probe_enabled

    def start_lag_probe(self) -> None:
        """Start the loop-lag sampler on the running loop; idempotent.

        Called from lifespan, not ``__init__``: the module gate is built at import
        time with no running loop, and an early start silently never schedules.
        """
        if self._lag_probe_enabled:
            self._lag_probe.start()

    def memory_ratio(self) -> float:
        return self.memory.usage_ratio()

    def cpu_ratio(self) -> float:
        return self.cpu.usage_ratio()

    def close(self) -> None:
        """Stop the optional loop-lag probe (call on shutdown)."""
        self._lag_probe.stop()

    def batch_scale_factor(self) -> float:
        base = min(self.memory.batch_scale_factor(), self.cpu.batch_scale_factor())
        # A lagging event loop is its own bottleneck regardless of cgroup CPU;
        # shed load proportionally when it falls behind its scheduled cadence.
        return base * self._lag_probe.factor()

    async def wait_while_saturated(self, max_wait_seconds: float | None = None) -> None:
        """Block while *either* resource is saturated, bounded by the budget.

        One injected ``clock`` bounds this wait and both sub-gates' waits; it is read
        back off ``memory`` (the budget's home) so the two cannot disagree.
        """
        budget = self.memory.max_wait_seconds if max_wait_seconds is None else max_wait_seconds
        deadline = self.memory.clock() + budget
        while True:
            mem_r = self.memory.usage_ratio()
            cpu_r = self.cpu.usage_ratio()
            if mem_r < self.memory.hard and cpu_r < self.cpu.hard:
                return
            if self.memory.clock() >= deadline:
                return
            await asyncio.sleep(self._poll_seconds)

    def http_scale_factor(self) -> float:
        """Scale factor for HTTP-only (browser-free) tasks.

        With ample CPU headroom (>50%) HTTP work rides the CPU-implied scale, escaping
        the browser-held-memory bottleneck; only when CPU is tight does it fall back.
        """
        cpu_headroom = self.cpu.headroom_fraction()
        if cpu_headroom > 0.5:
            # CPU has ample headroom -> HTTP tasks ride the CPU scale, escaping
            # the browser-held-memory bottleneck that throttles browser batches.
            return self.cpu.batch_scale_factor()
        # CPU is tight -> HTTP tasks respect combined pressure
        return self.batch_scale_factor()
